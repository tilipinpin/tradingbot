from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.telegram_commands import TelegramCommand
from src.telegram_notify import (
    AccountSnapshot,
    DiscordNotifier,
    TelegramNotifier,
    TradingNotificationService,
    build_discord_embed,
    calculate_daily_stats,
    estimated_crypto_taker_fee,
    fill_amounts,
    format_daily_message,
    format_fill_message,
    format_settlement_message,
    format_window_settlement_message,
    model_probability,
    position_value_breakdown,
    positions_for_market,
    sanitize_sensitive_text,
    settlement_key,
    settlement_values,
)


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, bool | str]:
        return {"ok": True, "id": "discord-message-1"}


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, float]] = []
        self.delete_calls: list[tuple[str, float]] = []

    def post(self, url: str, json: dict, timeout: float) -> FakeResponse:
        self.calls.append((url, json, timeout))
        return FakeResponse()

    def delete(self, url: str, timeout: float) -> FakeResponse:
        self.delete_calls.append((url, timeout))
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


def test_disabled_discord_notifier_is_a_noop() -> None:
    session = FakeSession()
    notifier = DiscordNotifier(None, session=session)

    assert notifier.send("hello") is False
    assert session.calls == []


def test_discord_notifier_sanitizes_embed_and_allows_user_role_mentions() -> None:
    session = FakeSession()
    webhook = "https://discord.com/api/webhooks/123456/secret_webhook_token"
    notifier = DiscordNotifier(webhook, username="Trading Bot", session=session)
    private_key = "0x" + "a" * 64

    assert notifier.send(f"failed with {private_key} via {webhook}") is True

    url, payload, timeout = session.calls[0]
    assert url == f"{webhook}?wait=true"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert private_key not in serialized
    assert webhook not in serialized
    assert "<private-key-redacted>" in serialized
    assert "<discord-webhook-redacted>" in serialized
    assert payload["allowed_mentions"] == {"parse": ["users", "roles"]}
    assert payload["username"] == "Trading Bot"
    assert payload["embeds"][0]["title"].startswith("failed with")
    assert "content" not in payload
    assert timeout == 10


def test_discord_notifier_sends_configured_mention() -> None:
    session = FakeSession()
    notifier = DiscordNotifier(
        "https://discord.com/api/webhooks/123456/secret_webhook_token",
        mention="<@&987654321>",
        allowed_mentions="users,roles",
        session=session,
    )

    assert notifier.send("⚠️ Polymarket 异常\n详情: RPC timeout") is True

    payload = session.calls[0][1]
    assert payload["content"] == "<@&987654321>"
    assert payload["allowed_mentions"] == {"parse": ["users", "roles"]}


def test_discord_plain_text_preserves_telegram_settlement_format() -> None:
    session = FakeSession()
    notifier = DiscordNotifier(
        "https://discord.com/api/webhooks/123456/secret_webhook_token",
        session=session,
    )
    message = (
        "🏁 Polymarket 交易已结算\n"
        "结果: ✅ 盈利\n"
        "本窗口净盈亏估算: +1.2500 pUSD\n"
        "市场: btc-updown-5m-1"
    )

    sent, message_id = notifier.send_with_message_id(
        message,
        preserve_text_format=True,
    )

    assert sent is True
    assert message_id == "discord-message-1"
    payload = session.calls[0][1]
    assert payload["content"] == message
    assert "embeds" not in payload


def test_discord_notifier_deletes_own_webhook_message() -> None:
    session = FakeSession()
    webhook = "https://discord.com/api/webhooks/123456/secret_webhook_token"
    notifier = DiscordNotifier(webhook, session=session)

    assert notifier.delete_message("987654321") is True
    assert session.delete_calls == [(f"{webhook}/messages/987654321", 10)]


def test_discord_embed_truncates_long_messages_and_posts_once() -> None:
    session = FakeSession()
    notifier = DiscordNotifier(
        "https://discord.com/api/webhooks/123456/secret_webhook_token",
        session=session,
    )

    assert notifier.send("通知\n" + "x" * 5000) is True

    assert len(session.calls) == 1
    assert len(session.calls[0][1]["embeds"][0]["description"]) == 4096


def test_discord_embed_uses_event_colors_and_structured_fields() -> None:
    winning = build_discord_embed(
        "🏁 Polymarket 交易已结算\n"
        "市场: btc-updown-5m-1\n"
        "买入方向: UP\n"
        "结果: ✅ 盈利\n"
        "本单毛盈亏: +1.2500 pUSD\n"
        "累计毛盈亏: +3.0000 pUSD"
    )
    losing = build_discord_embed("🏁 Polymarket 交易已结算\n结果: ❌ 亏损")
    error = build_discord_embed("⚠️ Polymarket 异常\n详情: RPC timeout")

    assert winning["color"] == 0x57F287
    assert losing["color"] == 0xED4245
    assert error["color"] == 0xED4245
    assert winning["title"] == "🏁 Polymarket 交易已结算 · ✅ 盈利 · 本单盈亏 +1.2500 pUSD"
    assert losing["title"] == "🏁 Polymarket 交易已结算 · ❌ 亏损"
    assert winning["fields"][0]["name"] == "市场"
    assert winning["fields"][1]["name"] == "买入方向"
    assert all(field["name"] != "结果" for field in winning["fields"])
    assert all(field["name"] != "本单毛盈亏" for field in winning["fields"])
    assert winning["footer"]["text"] == "Polymarket BTC 5m • 自动通知"


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
    assert "Taker 手续费估算: 0.0825 pUSD" in message
    assert "获胜时净收益估算: 1.8175 pUSD" in message
    assert "模型期望收益: -2.1825 pUSD" in message


def test_fee_and_account_equity_use_actual_fills_and_position_values() -> None:
    order = matched_order(cost="3.1", shares="5")
    assert estimated_crypto_taker_fee(order).quantize(Decimal("0.0001")) == Decimal("0.0825")

    active, redeemable = position_value_breakdown(
        [
            {"currentValue": "1.25", "redeemable": False},
            {"currentValue": "2.50", "redeemable": True},
            {"currentValue": "-1", "redeemable": True},
        ]
    )
    snapshot = AccountSnapshot(Decimal("20"), active, redeemable, 3)

    assert active == Decimal("1.25")
    assert redeemable == Decimal("2.50")
    assert snapshot.estimated_equity == Decimal("23.75")


def test_settlement_message_reports_realized_values_and_cumulative_stats() -> None:
    winning = settlement_values(matched_order(side="UP", cost="3.15", shares="5"), "UP")
    losing = settlement_values(matched_order(side="DOWN", cost="2.50", shares="5"), "UP")

    assert winning["payout"] == "5"
    assert winning["gross_pnl"] == "1.85"
    assert winning["return_rate"] == str(Decimal("1.85") / Decimal("3.15"))
    assert Decimal(winning["estimated_fee"]).quantize(Decimal("0.0001")) == Decimal("0.0816")
    assert losing["payout"] == "0"
    assert losing["gross_pnl"] == "-2.50"

    account = AccountSnapshot(Decimal("21.85"), Decimal("1.25"), Decimal("0.50"), 2)
    message = format_settlement_message(winning, account, [winning, losing])
    assert "结算方向: UP" in message
    assert "结果: ✅ 盈利" in message
    assert "结算返还: 5.0000 pUSD" in message
    assert "本单毛盈亏: +1.8500 pUSD" in message
    assert "本单净盈亏估算: +1.7684 pUSD" in message
    assert "可用 pUSD: 21.8500 pUSD" in message
    assert "估算总权益: 23.6000 pUSD" in message
    assert "累计结算: 2 单（1 胜 / 1 负）" in message
    assert "累计胜率: 50.00%" in message
    assert "累计毛盈亏: -0.6500 pUSD" in message
    assert "累计净盈亏估算: -0.8191 pUSD" in message


def test_window_settlement_message_combines_two_orders() -> None:
    first = settlement_values(matched_order(side="UP", cost="3.15", shares="5"), "UP")
    second_order = matched_order(side="DOWN", cost="2.50", shares="5")
    second_order["response"]["orderID"] = "0x22222222222222222222"
    second_order["order_role"] = "reverse_protection"
    second_order["reason"] = "protective_hedge primary_side=UP"
    second = settlement_values(second_order, "UP")
    account = AccountSnapshot(Decimal("21.85"), Decimal("0"), Decimal("0"), 0)

    message = format_window_settlement_message([first, second], account, [first, second])

    assert "本窗口成交: 2 单" in message
    assert "反向保护单: 1 单" in message
    assert "买入方向: UP × 1 / DOWN × 1" in message
    assert "窗口胜负: 1 胜 / 1 负" in message
    assert "订单 1 [首单]: UP" in message
    assert "订单 2 [反向保护单]: DOWN" in message
    assert "本窗口净盈亏估算: -0.8191 pUSD" in message


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


def test_settlement_service_sends_one_notification_per_window(tmp_path) -> None:
    session = FakeSession()
    notifier = TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session)
    ledger = tmp_path / "trades.jsonl"
    state = tmp_path / "state.json"
    first = matched_order(side="UP", cost="3.15", shares="5")
    second = matched_order(side="UP", cost="3.50", shares="5")
    second["response"]["orderID"] = "0x22222222222222222222"
    first["matched_at"] = "2026-07-16T20:44:16+00:00"
    second["matched_at"] = "2026-07-16T20:44:20+00:00"
    ledger.write_text("\n".join(json.dumps(order) for order in (first, second)) + "\n")
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
    assert "本窗口成交: 2 单" in session.calls[0][1]["text"]
    saved = json.loads(state.read_text())
    assert saved["settlement_windows"][first["slug"]]["notified_channels"] == ["telegram"]
    assert len(saved["settlements"]) == 2


def test_reversal_retained_position_uses_window_pnl_notification_on_both_channels(
    tmp_path,
) -> None:
    telegram_session = FakeSession()
    discord_session = FakeSession()
    ledger = tmp_path / "trades.jsonl"
    state = tmp_path / "state.json"
    service = TradingNotificationService(
        notifier=TelegramNotifier(
            "123456:telegram-token-value_1234567890",
            "42",
            session=telegram_session,
        ),
        discord_notifier=DiscordNotifier(
            "https://discord.com/api/webhooks/123456/secret_webhook_token",
            session=discord_session,
        ),
        trader=None,
        signature_type=3,
        strategy="reversal_v11",
        mode="live",
        version="test",
        summary={},
        ledger_path=ledger,
        state_path=state,
        settlement_interval=5,
        notify_on_matched=False,
    )
    service.record_reversal_exit(
        {
            "slug": "btc-updown-5m-700",
            "side": "UP",
            "retained_side": "DOWN",
            "split_amount": "2",
            "cumulative_exit_proceeds": "1.20",
            "round_id": 3,
            "attempt": 2,
            "exit_complete": True,
            "response": {
                "status": "matched",
                "makingAmount": "2",
                "takingAmount": "1.20",
            },
        }
    )

    ledger_order = json.loads(ledger.read_text().strip())
    assert ledger_order["side"] == "DOWN"
    assert ledger_order["order_role"] == "reversal_retained"
    assert ledger_order["response"]["makingAmount"] == "0.80"
    assert ledger_order["response"]["takingAmount"] == "2"

    service.maybe_send_settlements(lambda slug: "DOWN")

    assert len(telegram_session.calls) == 1
    telegram_text = telegram_session.calls[0][1]["text"]
    assert "结果: ✅ 盈利" in telegram_text
    assert "订单 1 [反转保留仓]: DOWN" in telegram_text
    assert "反转仓位: 卖出 UP / 保留 DOWN" in telegram_text
    assert "拆分金额: 2.0000 pUSD" in telegram_text
    assert "趋势仓卖出回款: 1.2000 pUSD" in telegram_text
    assert "轮次/阶段: 3/2" in telegram_text
    assert "本轮累计净盈亏估算: +1.1664 pUSD" in telegram_text
    assert len(discord_session.calls) == 1
    discord_payload = discord_session.calls[0][1]
    assert discord_payload["content"] == telegram_text
    assert "embeds" not in discord_payload
    saved = json.loads(state.read_text())
    assert saved["settlement_windows"]["btc-updown-5m-700"][
        "notified_channels"
    ] == ["discord", "telegram"]


def test_reversal_notification_round_pnl_includes_prior_attempt_losses() -> None:
    prior_order = matched_order(side="UP", cost="1.20", shares="2")
    prior_order.update(
        {
            "slug": "btc-updown-5m-695",
            "order_role": "reversal_retained",
            "round_id": 3,
            "attempt": 1,
        }
    )
    prior = settlement_values(prior_order, "DOWN")

    current_order = matched_order(side="DOWN", cost="0.80", shares="2")
    current_order.update(
        {
            "slug": "btc-updown-5m-700",
            "order_role": "reversal_retained",
            "round_id": 3,
            "attempt": 2,
        }
    )
    current = settlement_values(current_order, "DOWN")
    unrelated = dict(prior, round_id=2)

    message = format_window_settlement_message(
        [current],
        AccountSnapshot(Decimal("20"), Decimal("0"), Decimal("0"), 0),
        [unrelated, prior, current],
    )

    assert "轮次/阶段: 3/2" in message
    assert "本轮累计净盈亏估算: -0.0672 pUSD" in message
    lines = message.splitlines()
    window_pnl_index = lines.index("本窗口净盈亏估算: +1.1664 pUSD")
    assert lines[window_pnl_index + 1 : window_pnl_index + 3] == [
        "轮次/阶段: 3/2",
        "本轮累计净盈亏估算: -0.0672 pUSD",
    ]


def test_settlement_window_migration_does_not_repeat_legacy_notifications(tmp_path) -> None:
    session = FakeSession()
    notifier = TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session)
    ledger = tmp_path / "trades.jsonl"
    state = tmp_path / "state.json"
    first = matched_order(side="UP", cost="3.15", shares="5")
    second = matched_order(side="UP", cost="3.50", shares="5")
    second["response"]["orderID"] = "0x22222222222222222222"
    ledger.write_text("\n".join(json.dumps(order) for order in (first, second)) + "\n")
    records = {}
    for order in (first, second):
        record = settlement_values(order, "UP")
        record["notified_channels"] = ["telegram"]
        records[settlement_key(order)] = record
    state.write_text(json.dumps({"days": {}, "settlements": records}))
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

    service.maybe_send_settlements(lambda slug: None)

    assert session.calls == []
    saved = json.loads(state.read_text())
    window = saved["settlement_windows"][first["slug"]]
    assert window["notified_channels"] == ["telegram"]
    assert window["migrated_from_individual"] is True


def test_start_notification_is_sent_to_telegram_and_discord(tmp_path) -> None:
    telegram_session = FakeSession()
    discord_session = FakeSession()
    service = TradingNotificationService(
        notifier=TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=telegram_session),
        discord_notifier=DiscordNotifier(
            "https://discord.com/api/webhooks/123456/secret_webhook_token",
            session=discord_session,
        ),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="live",
        version="test",
        summary={},
        state_path=tmp_path / "state.json",
    )

    service.start()

    assert "机器人启动成功" in telegram_session.calls[0][1]["text"]
    assert "机器人启动成功" in discord_session.calls[0][1]["embeds"][0]["title"]
    assert "reply_markup" in telegram_session.calls[0][1]
    assert "reply_markup" not in discord_session.calls[0][1]
    saved = json.loads((tmp_path / "state.json").read_text())
    queued = saved["discord_message_deletions"]
    assert len(queued) == 1
    assert queued[0]["message_id"] == "discord-message-1"
    assert queued[0]["kind"] == "start"


def test_discord_deletion_waits_until_deadline_and_survives_restart(tmp_path, monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("src.telegram_notify.time.time", lambda: now[0])
    state = tmp_path / "state.json"
    first_session = FakeSession()
    first = TradingNotificationService(
        notifier=TelegramNotifier(None, None),
        discord_notifier=DiscordNotifier(
            "https://discord.com/api/webhooks/123456/secret_webhook_token",
            session=first_session,
        ),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="live",
        version="test",
        summary={},
        state_path=state,
        discord_start_stop_retention_seconds=300,
    )
    first.start()

    restarted_session = FakeSession()
    restarted = TradingNotificationService(
        notifier=TelegramNotifier(None, None),
        discord_notifier=DiscordNotifier(
            "https://discord.com/api/webhooks/123456/secret_webhook_token",
            session=restarted_session,
        ),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="live",
        version="test",
        summary={},
        state_path=state,
    )
    now[0] = 1299.0
    restarted.maybe_delete_expired_discord_messages()
    assert restarted_session.delete_calls == []

    now[0] = 1300.0
    restarted.maybe_delete_expired_discord_messages()
    assert len(restarted_session.delete_calls) == 1
    assert json.loads(state.read_text())["discord_message_deletions"] == []


def test_settlement_retries_only_missing_discord_delivery(tmp_path) -> None:
    class FlakyResponse(FakeResponse):
        def __init__(self, should_fail: bool) -> None:
            self.should_fail = should_fail

        def raise_for_status(self) -> None:
            if self.should_fail:
                raise RuntimeError("temporary Discord outage")

    class FlakyDiscordSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.should_fail = True

        def post(self, url: str, json: dict, timeout: float) -> FlakyResponse:
            self.calls.append((url, json, timeout))
            return FlakyResponse(self.should_fail)

    telegram_session = FakeSession()
    discord_session = FlakyDiscordSession()
    ledger = tmp_path / "trades.jsonl"
    state = tmp_path / "state.json"
    order = matched_order()
    order["matched_at"] = "2026-07-16T20:44:16+00:00"
    ledger.write_text(json.dumps(order) + "\n")
    service = TradingNotificationService(
        notifier=TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=telegram_session),
        discord_notifier=DiscordNotifier(
            "https://discord.com/api/webhooks/123456/secret_webhook_token",
            session=discord_session,
        ),
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

    key = settlement_key(order)
    saved = json.loads(state.read_text())
    assert saved["settlements"][key]["notified_channels"] == ["telegram"]
    assert len(telegram_session.calls) == 1
    assert len(discord_session.calls) == 1

    discord_session.should_fail = False
    service._next_settlement_attempt = 0
    service.maybe_send_settlements(lambda slug: None)

    saved = json.loads(state.read_text())
    assert saved["settlements"][key]["notified_channels"] == ["discord", "telegram"]
    assert saved["discord_message_deletions"][0]["kind"] == "settlement"
    assert (
        saved["discord_message_deletions"][0]["delete_at"]
        - datetime.fromisoformat(saved["discord_message_deletions"][0]["created_at"]).timestamp()
    ) == pytest.approx(259200, abs=2)
    assert len(telegram_session.calls) == 1
    assert len(discord_session.calls) == 2


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


def test_discord_ignores_matched_and_exceptions_but_keeps_daily_report(tmp_path) -> None:
    telegram_session = FakeSession()
    discord_session = FakeSession()
    ledger = tmp_path / "trades.jsonl"
    state = tmp_path / "state.json"
    report_day = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    order = matched_order()
    order["matched_at"] = datetime.combine(report_day, datetime.min.time(), ZoneInfo("Asia/Shanghai")).isoformat()
    ledger.write_text(json.dumps(order) + "\n")
    state.write_text(
        json.dumps(
            {
                "days": {
                    report_day.isoformat(): {
                        "reported": False,
                        "start_balance": "20",
                        "end_balance": "21",
                    }
                },
                "settlements": {},
            }
        )
    )
    service = TradingNotificationService(
        notifier=TelegramNotifier(
            "123456:telegram-token-value_1234567890",
            "42",
            session=telegram_session,
        ),
        discord_notifier=DiscordNotifier(
            "https://discord.com/api/webhooks/123456/secret_webhook_token",
            session=discord_session,
        ),
        trader=None,
        signature_type=3,
        strategy="fair_value_edge",
        mode="live",
        version="test",
        summary={},
        ledger_path=ledger,
        state_path=state,
        notify_on_matched=True,
    )
    service.current_account_snapshot = lambda: AccountSnapshot(
        Decimal("22"), Decimal("0"), Decimal("0")
    )

    service.record_fill(matched_order())
    service.notify_exception("RPC", RuntimeError("timeout"), cooldown=0)
    service.maybe_send_daily(lambda slug: "UP")

    telegram_messages = [call[1]["text"] for call in telegram_session.calls]
    assert any("实际成交" in message for message in telegram_messages)
    assert any("机器人异常" in message for message in telegram_messages)
    assert any("日报" in message for message in telegram_messages)
    assert len(discord_session.calls) == 1
    assert "日报" in discord_session.calls[0][1]["embeds"][0]["title"]
    saved = json.loads(state.read_text())
    assert saved["discord_message_deletions"] == []


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
    assert stats.settled_estimated_fees == Decimal("0.154")
    assert stats.estimated_fees == Decimal("0.189")
    assert stats.net_pnl == Decimal("-0.154")

    message = format_daily_message(date(2026, 7, 16), stats, Decimal("20"), Decimal("20"))
    assert "实际成交: 3 单（已结算 2，待结算 1）" in message
    assert "胜率: 50.00%" in message
    assert "已结算手续费估算: 0.1540 pUSD" in message
    assert "已结算净盈亏估算: -0.1540 pUSD" in message
    assert "全部成交手续费估算: 0.1890 pUSD" in message


def test_sensitive_text_redacts_telegram_tokens() -> None:
    token = "123456789:abcdefghijklmnopqrstuvwxyz_ABCD"
    assert token not in sanitize_sensitive_text(f"request {token} failed")
    webhook = "https://discord.com/api/webhooks/123456/secret_webhook_token"
    assert webhook not in sanitize_sensitive_text(f"request {webhook} failed")


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
    assert "已结算手续费估算: 0.0840 pUSD" in message
    assert "已结算净盈亏估算: +1.9160 pUSD" in message
    assert "可用 pUSD: 22.0000 pUSD" in message


def test_positions_command_shows_only_current_market(monkeypatch, tmp_path) -> None:
    class PositionResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict]:
            return [
                {
                    "title": "Bitcoin Up or Down - current",
                    "eventSlug": "btc-updown-5m-current",
                    "outcome": "Up",
                    "size": 5,
                    "avgPrice": 0.6,
                    "curPrice": 0.7,
                    "currentValue": 3.5,
                    "cashPnl": 0.5,
                    "redeemable": False,
                },
                {
                    "title": "Bitcoin Up or Down - historical",
                    "eventSlug": "btc-updown-5m-old",
                    "outcome": "Down",
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
    service.update_runtime(slug="btc-updown-5m-current")
    service._command_poller = StubCommandPoller([TelegramCommand(1, "/positions")], offset=2)

    service.process_commands()

    assert captured["params"]["user"] == "0xdeposit"
    message = session.calls[0][1]["text"]
    assert "当前窗口持仓（1 项）" in message
    assert "市场: btc-updown-5m-current" in message
    assert "UP | 5.0000 份" in message
    assert "盈亏 +0.5000 pUSD" in message
    assert "historical" not in message
    assert "可赎回" not in message


def test_positions_for_market_rejects_history_and_unknown_runtime_market() -> None:
    positions = [
        {"eventSlug": "current", "outcome": "Up"},
        {"slug": "old", "outcome": "Down", "redeemable": True},
    ]

    assert positions_for_market(positions, "current") == [positions[0]]
    assert positions_for_market(positions, None) == []


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


def test_paper_reversal_strategy_is_queued_and_activated(tmp_path) -> None:
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

    service._queue_strategy("reversal_v11")

    assert service.strategy == "fair_value_edge"
    assert service.pending_strategy == "reversal_v11"
    assert service.activate_pending_strategy("btc-updown-5m-2") == "reversal_v11"
    assert service.strategy == "reversal_v11"


def test_live_strategy_switch_queues_reversal_v11(tmp_path) -> None:
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

    service._queue_strategy("reversal_v11")

    assert service.pending_strategy == "reversal_v11"
    assert "反转·2窗" in session.calls[-1][1]["text"]


def test_strategy_override_persists_for_paper_and_live(tmp_path) -> None:
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
    paper.state.setdefault("control", {})["strategy_override"] = "reversal_v11"
    paper._save_state()

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

    assert restarted_paper.resolve_effective_strategy() == "reversal_v11"
    assert restarted_live.resolve_effective_strategy() == "reversal_v11"


def test_queued_reversal_strategy_survives_restart(tmp_path) -> None:
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
    service._queue_strategy("reversal_v11")

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
    assert restarted.pending_strategy == "reversal_v11"
    assert restarted.activate_pending_strategy("btc-updown-5m-2") == "reversal_v11"


def test_manual_buy_confirmation_queues_independent_two_share_request(tmp_path) -> None:
    session = FakeSession()
    submitted = []
    service = TradingNotificationService(
        notifier=TelegramNotifier("123456:telegram-token-value_1234567890", "42", session=session),
        trader=None,
        signature_type=3,
        strategy="reversal_v11_four_streak",
        mode="live",
        version="test",
        summary={},
        state_path=tmp_path / "state.json",
    )
    service.attach_manual_executor(submitted.append)
    service.update_runtime(slug="btc-updown-5m-900")

    service._send_manual_confirmation("current_buy_up")
    service._queue_manual_trade("current_buy_up", "77")

    assert len(submitted) == 1
    assert submitted[0].request_id == "telegram-77"
    assert submitted[0].target_slug == "btc-updown-5m-900"
    assert submitted[0].action == "buy"
    assert submitted[0].side == "UP"
    assert service.state["manual_orders"][0]["status"] == "queued"
    assert service.strategy == "reversal_v11_four_streak"


def test_manual_next_window_buy_targets_exact_next_slug(tmp_path) -> None:
    submitted = []
    service = TradingNotificationService(
        notifier=TelegramNotifier(None, None),
        trader=None,
        signature_type=3,
        strategy="reversal_v11_four_streak",
        mode="live",
        version="test",
        summary={},
        state_path=tmp_path / "state.json",
    )
    service.attach_manual_executor(submitted.append)
    service.update_runtime(slug="btc-updown-5m-900")

    service._send_manual_confirmation("next_buy_down")
    service._queue_manual_trade("next_buy_down", "78")

    assert submitted[0].target_slug == "btc-updown-5m-1200"
    assert submitted[0].side == "DOWN"


def test_manual_current_window_confirmation_expires_after_window_change(tmp_path) -> None:
    submitted = []
    service = TradingNotificationService(
        notifier=TelegramNotifier(None, None),
        trader=None,
        signature_type=3,
        strategy="reversal_v11_four_streak",
        mode="live",
        version="test",
        summary={},
        state_path=tmp_path / "state.json",
    )
    service.attach_manual_executor(submitted.append)
    service.update_runtime(slug="btc-updown-5m-900")
    service.state["manual_orders"] = [
        {
            "request_id": "prior-buy",
            "target_slug": "btc-updown-5m-900",
            "action": "buy",
            "side": "UP",
            "status": "matched",
            "filled_size": "2",
        }
    ]
    service._send_manual_confirmation("current_sell_up")
    service.update_runtime(slug="btc-updown-5m-1200")

    service._queue_manual_trade("current_sell_up", "79")

    assert submitted == []
    assert [item["request_id"] for item in service.state["manual_orders"]] == [
        "prior-buy"
    ]
