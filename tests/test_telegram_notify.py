from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from src.telegram_commands import TelegramCommand
from src.telegram_notify import (
    TelegramNotifier,
    TradingNotificationService,
    calculate_daily_stats,
    fill_amounts,
    format_daily_message,
    format_fill_message,
    format_settlement_message,
    model_probability,
    sanitize_sensitive_text,
    settlement_key,
    settlement_values,
)


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, bool]:
        return {"ok": True}


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, float]] = []

    def post(self, url: str, json: dict, timeout: float) -> FakeResponse:
        self.calls.append((url, json, timeout))
        return FakeResponse()


class StubCommandPoller:
    def __init__(self, commands: list[TelegramCommand], offset: int = 1) -> None:
        self.commands = commands
        self.offset = offset
        self.drained_offset = offset

    def drain(self) -> list[TelegramCommand]:
        commands = self.commands
        self.commands = []
        return commands

    def stop(self, wait: bool = True) -> None:
        return None


def matched_order(
    slug: str = "btc-updown-5m-1",
    side: str = "UP",
    cost: str = "3",
    shares: str = "5",
) -> dict:
    return {
        "slug": slug,
        "side": side,
        "price": "0.60",
        "size": "5",
        "notional": "3",
        "reason": "fair_value_edge p_up=0.8000",
        "response": {
            "success": True,
            "status": "matched",
            "orderID": "0x1234567890abcdef1234",
            "makingAmount": cost,
            "takingAmount": shares,
        },
    }


def test_disabled_notifier_is_a_noop() -> None:
    session = FakeSession()
    notifier = TelegramNotifier(None, None, session=session)
    assert notifier.send("hello") is False
    assert session.calls == []


def test_notifier_posts_message_and_redacts_private_keys() -> None:
    session = FakeSession()
    notifier = TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session)
    private_key = "0x" + "a" * 64

    assert notifier.send(f"failed with {private_key}") is True

    url, payload, timeout = session.calls[0]
    assert url.endswith("/sendMessage")
    assert payload["chat_id"] == "42"
    assert private_key not in payload["text"]
    assert "<private-key-redacted>" in payload["text"]
    assert timeout == 10


def test_notifier_sends_persistent_reply_keyboard() -> None:
    session = FakeSession()
    notifier = TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session)
    markup = {"keyboard": [[{"text": "📈 查看余额"}]], "is_persistent": True}

    assert notifier.send("controls", reply_markup=markup) is True

    assert session.calls[0][1]["reply_markup"] == markup


def test_alert_cooldown_suppresses_duplicate(monkeypatch) -> None:
    session = FakeSession()
    notifier = TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session)
    times = iter([100.0, 101.0, 500.0])
    monkeypatch.setattr("src.telegram_notify.time.monotonic", lambda: next(times))

    assert notifier.alert("network", "first", cooldown=300) is True
    assert notifier.alert("network", "duplicate", cooldown=300) is False
    assert notifier.alert("network", "recovered then failed", cooldown=300) is True
    assert len(session.calls) == 2


def test_fill_message_uses_actual_matched_amounts_and_probability() -> None:
    order = matched_order(side="DOWN", cost="3.1", shares="5")
    assert fill_amounts(order) == (Decimal("3.1"), Decimal("5"), Decimal("0.62"))
    assert model_probability(order) == Decimal("0.2000")

    message = format_fill_message(order)
    assert "成交均价: 0.6200" in message
    assert "数量: 5.0000 份" in message
    assert "获胜时毛收益: 1.9000 pUSD" in message
    assert "模型期望收益: -2.1000 pUSD" in message


def test_settlement_message_reports_realized_values_and_cumulative_stats() -> None:
    winning = settlement_values(matched_order(side="UP", cost="3.15", shares="5"), "UP")
    losing = settlement_values(matched_order(side="DOWN", cost="2.50", shares="5"), "UP")

    assert winning["payout"] == "5"
    assert winning["gross_pnl"] == "1.85"
    assert winning["return_rate"] == str(Decimal("1.85") / Decimal("3.15"))
    assert losing["payout"] == "0"
    assert losing["gross_pnl"] == "-2.50"

    message = format_settlement_message(winning, Decimal("21.85"), [winning, losing])
    assert "结算方向: UP" in message
    assert "结果: ✅ 盈利" in message
    assert "结算返还: 5.0000 pUSD" in message
    assert "本单毛盈亏: +1.8500 pUSD" in message
    assert "累计结算: 2 单（1 胜 / 1 负）" in message
    assert "累计胜率: 50.00%" in message
    assert "累计毛盈亏: -0.6500 pUSD" in message


def test_settlement_service_persists_and_deduplicates_notifications(tmp_path) -> None:
    session = FakeSession()
    notifier = TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session)
    ledger = tmp_path / "trades.jsonl"
    state = tmp_path / "state.json"
    order = matched_order()
    order["matched_at"] = "2026-07-16T20:44:16+00:00"
    ledger.write_text(json.dumps(order) + "\n")
    service = TradingNotificationService(
        notifier=notifier,
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="live",
        version="test",
        summary={},
        ledger_path=ledger,
        state_path=state,
        settlement_interval=5,
    )

    service.maybe_send_settlements(lambda slug: "UP")
    service._next_settlement_attempt = 0
    service.maybe_send_settlements(lambda slug: "UP")

    assert len(session.calls) == 1
    assert "交易已结算" in session.calls[0][1]["text"]
    saved = json.loads(state.read_text())
    assert settlement_key(order) in saved["settlements"]


def test_record_fill_is_silent_until_settlement_by_default(tmp_path) -> None:
    session = FakeSession()
    service = TradingNotificationService(
        notifier=TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="live",
        version="test",
        summary={},
        ledger_path=tmp_path / "trades.jsonl",
        state_path=tmp_path / "state.json",
    )

    service.record_fill(matched_order())

    assert session.calls == []
    assert service.ledger_path.read_text().strip()


def test_daily_stats_count_only_resolved_orders_in_win_rate() -> None:
    orders = [
        matched_order("one", "UP", "3", "5"),
        matched_order("two", "DOWN", "2", "4"),
        matched_order("three", "UP", "1", "2"),
    ]
    stats = calculate_daily_stats(orders, {"one": "UP", "two": "UP", "three": None})

    assert stats.fills == 3
    assert stats.settled == 2
    assert stats.wins == 1
    assert stats.losses == 1
    assert stats.unresolved == 1
    assert stats.win_rate == Decimal("0.5")
    assert stats.gross_pnl == Decimal("0")

    message = format_daily_message(date(2026, 7, 16), stats, Decimal("20"), Decimal("20"))
    assert "实际成交: 3 单（已结算 2，待结算 1）" in message
    assert "胜率: 50.00%" in message
    assert "手续费/余额差额估算: N/A" in message


def test_sensitive_text_redacts_telegram_tokens() -> None:
    token = "123456789:abcdefghijklmnopqrstuvwxyz_ABCD"
    assert token not in sanitize_sensitive_text(f"request {token} failed")


def test_command_stop_and_start_persist_trading_gate(tmp_path) -> None:
    session = FakeSession()
    state = tmp_path / "state.json"
    service = TradingNotificationService(
        notifier=TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="live",
        version="test",
        summary={},
        state_path=state,
    )
    service._command_poller = StubCommandPoller(
        [TelegramCommand(1, "/stop"), TelegramCommand(2, "/start")],
        offset=3,
    )

    assert service.process_commands() is False

    assert service.trading_paused is False
    saved = json.loads(state.read_text())
    assert saved["control"]["paused"] is False
    assert saved["telegram"]["offset"] == 3
    assert "已暂停自动交易" in session.calls[0][1]["text"]
    assert "已恢复自动交易" in session.calls[1][1]["text"]


def test_restart_command_requests_process_replacement_without_persisting_pause(tmp_path) -> None:
    session = FakeSession()
    state = tmp_path / "state.json"
    service = TradingNotificationService(
        notifier=TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="live",
        version="test",
        summary={},
        state_path=state,
    )
    service._command_poller = StubCommandPoller([TelegramCommand(4, "/restart")], offset=5)

    assert service.process_commands() is True

    saved = json.loads(state.read_text())
    assert saved["telegram"]["offset"] == 5
    assert saved.get("control", {}).get("paused") is not True
    assert "正在保存状态并重启" in session.calls[0][1]["text"]


def test_today_pnl_uses_persisted_per_order_settlements(tmp_path) -> None:
    session = FakeSession()
    ledger = tmp_path / "trades.jsonl"
    state = tmp_path / "state.json"
    order = matched_order(cost="3", shares="5")
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    order["matched_at"] = now.isoformat()
    ledger.write_text(json.dumps(order) + "\n")
    state.write_text(
        json.dumps(
            {
                "days": {},
                "settlements": {settlement_key(order): settlement_values(order, "UP")},
            }
        )
    )
    service = TradingNotificationService(
        notifier=TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="live",
        version="test",
        summary={},
        ledger_path=ledger,
        state_path=state,
    )
    service.current_balance = lambda: Decimal("22")
    service._command_poller = StubCommandPoller([TelegramCommand(1, "/pnl")], offset=2)

    service.process_commands()

    message = session.calls[0][1]["text"]
    assert "实际成交: 1 单" in message
    assert "胜率: 100.00%" in message
    assert "已结算毛盈亏: +2.0000 pUSD" in message
    assert "当前钱包余额: 22.0000 pUSD" in message


def test_positions_command_uses_deposit_wallet_data_api(monkeypatch, tmp_path) -> None:
    class PositionResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict]:
            return [
                {
                    "title": "Bitcoin Up or Down - test",
                    "outcome": "Up",
                    "size": 5,
                    "avgPrice": 0.6,
                    "curPrice": 1,
                    "currentValue": 5,
                    "cashPnl": 2,
                    "redeemable": True,
                }
            ]

    captured: dict = {}

    def fake_get(url: str, params: dict, timeout: float) -> PositionResponse:
        captured.update({"url": url, "params": params, "timeout": timeout})
        return PositionResponse()

    monkeypatch.setattr("src.telegram_notify.requests.get", fake_get)
    session = FakeSession()
    service = TradingNotificationService(
        notifier=TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="live",
        version="test",
        summary={},
        state_path=tmp_path / "state.json",
        wallet_address="0xdeposit",
    )
    service._command_poller = StubCommandPoller([TelegramCommand(1, "/positions")], offset=2)

    service.process_commands()

    assert captured["params"]["user"] == "0xdeposit"
    message = session.calls[0][1]["text"]
    assert "当前持仓（1 项）" in message
    assert "UP | 5.0000 份" in message
    assert "盈亏 +2.0000 pUSD | 可赎回" in message


def test_status_command_reports_health_and_trading_pause(tmp_path) -> None:
    session = FakeSession()
    service = TradingNotificationService(
        notifier=TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="live",
        version="0.7.0",
        summary={"order_attempts": 3, "matched_orders": 1},
        state_path=tmp_path / "state.json",
    )
    service._set_trading_paused(True)
    service.update_runtime(
        slug="btc-updown-5m-1",
        seconds_left=Decimal("42"),
        spot=Decimal("118000.50"),
        spot_source="CHAINLINK",
    )
    service._command_poller = StubCommandPoller([TelegramCommand(1, "/status")], offset=2)

    service.process_commands()

    message = session.calls[0][1]["text"]
    assert "进程: 正常运行" in message
    assert "交易: 已暂停" in message
    assert "累计尝试/成交: 3/1" in message
    assert "窗口剩余: 42 秒" in message
    assert "BTC/USD: 118000.50（CHAINLINK）" in message


def test_paper_strategy_switch_is_queued_and_activated_next_window(tmp_path) -> None:
    session = FakeSession()
    state = tmp_path / "state.json"
    service = TradingNotificationService(
        notifier=TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="paper",
        version="test",
        summary={},
        state_path=state,
    )

    service._queue_strategy("late_favorite")

    assert service.strategy == "fair_value_edge"
    assert service.pending_strategy == "late_favorite"
    assert service.activate_pending_strategy("btc-updown-5m-2") == "late_favorite"
    assert service.strategy == "late_favorite"
    saved = json.loads(state.read_text())
    assert saved["control"]["strategy_override"] == "late_favorite"
    assert "pending_strategy" not in saved["control"]
    assert "生效市场: btc-updown-5m-2" in session.calls[-1][1]["text"]


def test_live_strategy_switch_rejects_paper_only_strategy(tmp_path) -> None:
    session = FakeSession()
    service = TradingNotificationService(
        notifier=TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="live",
        version="test",
        summary={},
        state_path=tmp_path / "state.json",
    )

    service._queue_strategy("late_favorite")

    assert service.pending_strategy is None
    assert "仅允许纸面或 dry-run" in session.calls[-1][1]["text"]


def test_strategy_override_persists_for_paper_and_is_ignored_for_live(tmp_path) -> None:
    state = tmp_path / "state.json"
    paper = TradingNotificationService(
        notifier=TelegramNotifier(None, None),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="paper",
        version="test",
        summary={},
        state_path=state,
    )
    paper._queue_strategy("late_favorite")
    paper.activate_pending_strategy("btc-updown-5m-2")

    restarted_paper = TradingNotificationService(
        notifier=TelegramNotifier(None, None),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="paper",
        version="test",
        summary={},
        state_path=state,
    )
    restarted_live = TradingNotificationService(
        notifier=TelegramNotifier(None, None),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="live",
        version="test",
        summary={},
        state_path=state,
    )

    assert restarted_paper.resolve_effective_strategy() == "late_favorite"
    assert restarted_live.resolve_effective_strategy() == "fair_value_edge"


def test_queued_strategy_survives_restart_without_switching_same_window(tmp_path) -> None:
    state = tmp_path / "state.json"
    service = TradingNotificationService(
        notifier=TelegramNotifier(None, None),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="paper",
        version="test",
        summary={},
        state_path=state,
    )
    service.update_runtime(slug="btc-updown-5m-1")
    service._queue_strategy("late_favorite")

    restarted = TradingNotificationService(
        notifier=TelegramNotifier(None, None),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="paper",
        version="test",
        summary={},
        state_path=state,
    )

    assert restarted.activate_pending_strategy("btc-updown-5m-1") is None
    assert restarted.pending_strategy == "late_favorite"
    assert restarted.activate_pending_strategy("btc-updown-5m-2") == "late_favorite"
