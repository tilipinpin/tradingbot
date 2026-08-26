from __future__ import annotations

from threading import Event

from src.telegram_commands import (
    DEFAULT_STRATEGY,
    TelegramCommandPoller,
    reply_keyboard_markup,
    strategy_selection_markup,
)


class FakeNotifier:
    def __init__(self, updates: list[dict]) -> None:
        self.updates = updates
        self.calls: list[tuple[str, dict, float | None]] = []

    def api_request(
        self,
        method: str,
        payload: dict,
        timeout: float | None = None,
    ) -> dict:
        self.calls.append((method, payload, timeout))
        return {"ok": True, "result": self.updates}


def telegram_update(update_id: int, chat_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


def telegram_callback(update_id: int, chat_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": chat_id},
            "message": {"chat": {"id": chat_id}},
            "data": data,
        },
    }


def test_poller_accepts_only_configured_chat_and_advances_offset() -> None:
    notifier = FakeNotifier(
        [
            telegram_update(10, 99, "/stop"),
            telegram_update(11, 42, "/balance@tilibot"),
            telegram_update(12, 42, "hello"),
        ]
    )
    pause_event = Event()
    poller = TelegramCommandPoller(notifier, "42", 10, pause_event, poll_timeout=5)

    assert poller.poll_once() is True

    assert poller.offset == 13
    assert [item.command for item in poller.drain()] == ["/balance"]
    assert pause_event.is_set() is False
    assert notifier.calls[0][1]["offset"] == 10


def test_stop_start_and_restart_change_immediate_order_gate() -> None:
    notifier = FakeNotifier(
        [
            telegram_update(1, 42, "/stop"),
            telegram_update(2, 42, "/start"),
            telegram_update(3, 42, "/restart"),
        ]
    )
    pause_event = Event()
    poller = TelegramCommandPoller(notifier, "42", 0, pause_event)

    poller.poll_once()

    assert pause_event.is_set() is True
    assert [item.command for item in poller.drain()] == ["/stop", "/start", "/restart"]


def test_poller_ignores_unknown_commands() -> None:
    notifier = FakeNotifier([telegram_update(7, 42, "/withdraw")])
    poller = TelegramCommandPoller(notifier, "42", 0, Event())

    poller.poll_once()

    assert poller.offset == 8
    assert poller.drain() == []


def test_poller_discards_first_successful_backlog_after_startup_failure() -> None:
    notifier = FakeNotifier([telegram_update(20, 42, "/restart")])
    pause_event = Event()
    poller = TelegramCommandPoller(
        notifier,
        "42",
        0,
        pause_event,
        discard_pending=True,
    )

    poller.poll_once()

    assert poller.offset == 21
    assert poller.drain() == []
    assert pause_event.is_set() is False
    assert poller.discard_pending is False


def test_persistent_keyboard_has_all_control_buttons() -> None:
    markup = reply_keyboard_markup()
    labels = [button["text"] for row in markup["keyboard"] for button in row]

    assert markup["is_persistent"] is True
    assert markup["resize_keyboard"] is True
    assert labels == [
        "📈 查看余额",
        "📊 今日盈亏",
        "📋 查看持仓",
        "❤️ 运行状态",
        "⛔ 停止交易",
        "▶️ 恢复交易",
        "🧠 选择策略",
        "🎛 手动交易",
    ]


def test_poller_maps_large_keyboard_button_to_command() -> None:
    notifier = FakeNotifier([telegram_update(30, 42, "📋 查看持仓")])
    poller = TelegramCommandPoller(notifier, "42", 30, Event())

    poller.poll_once()

    assert [item.command for item in poller.drain()] == ["/positions"]


def test_live_strategy_menu_keeps_combined_strategy_and_adds_reversal_v11() -> None:
    markup = strategy_selection_markup("live", "fair_value_edge", None)
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    by_data = {button["callback_data"]: button["text"] for button in buttons}
    callbacks = [button["callback_data"] for button in buttons]

    assert "✅ 公允价值差" == by_data["strategy:select:fair_value_edge"]
    assert "盘口波动套利" == by_data[
        "strategy:select:fast_directional_hedge_simple"
    ]
    assert "strategy:select:open_060_late_070" not in by_data
    assert "智能评分" == by_data["strategy:select:smart_score"]
    assert "EWMA·TWAP公平价" == by_data["strategy:select:ewma_twap_fair"]
    assert "动量确认" == by_data["strategy:select:momentum_confirmation"]
    assert "4窗反转·64U" == by_data[
        "strategy:select:reversal_four_64"
    ]
    assert "5窗反转·10阶" == by_data[
        "strategy:select:reversal_v11"
    ]
    assert "4窗反转·10阶" == by_data[
        "strategy:select:reversal_v11_four_streak"
    ]
    assert "4窗反转·4-0-0追回" == by_data[
        "strategy:select:reversal_v11_six_streak"
    ]
    assert "strategy:select:reversal_v11_seven_streak" not in by_data
    assert "strategy:select:reversal_v11_eight_streak" not in by_data
    assert "strategy:select:reversal_v11_three_streak" not in by_data
    assert "strategy:select:reversal_compact" not in by_data
    assert "反转·首段" == by_data[
        "strategy:select:reversal_first_stage"
    ]
    assert "3窗反转·16U" == by_data[
        "strategy:select:reversal_three_16"
    ]
    assert "2窗反转·12阶" == by_data[
        "strategy:select:reversal_three_4_8"
    ]
    assert "strategy:select:spread_market_maker" not in by_data
    assert "strategy:select:late_070" not in by_data
    assert "strategy:select:late_one_way" not in by_data
    assert "strategy:select:open_060" not in by_data
    assert "↩️ 恢复启动策略" == by_data[f"strategy:select:{DEFAULT_STRATEGY}"]
    assert len(buttons) == 14
    assert callbacks.index("strategy:select:reversal_first_stage") == 5
    assert callbacks.index("strategy:select:reversal_four_64") == 10


def test_paper_strategy_menu_allows_smart_score() -> None:
    markup = strategy_selection_markup("paper", "fair_value_edge", None)
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    by_data = {button["callback_data"]: button["text"] for button in buttons}

    assert by_data["strategy:select:smart_score"] == "智能评分"
    assert by_data["strategy:select:ewma_twap_fair"] == "EWMA·TWAP公平价"
    assert by_data["strategy:select:fast_directional_hedge_simple"] == "盘口波动套利"
    assert by_data["strategy:select:momentum_confirmation"] == "动量确认"
    assert by_data["strategy:select:reversal_four_64"] == "4窗反转·64U"
    assert by_data["strategy:select:reversal_v11"] == "5窗反转·10阶"
    assert by_data["strategy:select:reversal_v11_four_streak"] == (
        "4窗反转·10阶"
    )
    assert by_data["strategy:select:reversal_v11_six_streak"] == "4窗反转·4-0-0追回"
    assert by_data["strategy:select:reversal_three_4_8"] == "2窗反转·12阶"
    assert by_data["strategy:select:reversal_three_16"] == "3窗反转·16U"
    assert "strategy:select:reversal_v11_seven_streak" not in by_data
    assert "strategy:select:reversal_compact" not in by_data
    assert "strategy:select:reversal_v11_eight_streak" not in by_data
    assert "strategy:select:reversal_v11_three_streak" not in by_data


def test_poller_parses_strategy_callback_from_authorized_chat() -> None:
    notifier = FakeNotifier([telegram_callback(40, 42, "strategy:select:reversal_v11")])
    poller = TelegramCommandPoller(notifier, "42", 40, Event())

    poller.poll_once()
    commands = poller.drain()

    assert len(commands) == 1
    assert commands[0].command == "/strategy_select"
    assert commands[0].argument == "reversal_v11"
    assert commands[0].callback_query_id == "callback-40"


def test_poller_parses_manual_trade_callbacks() -> None:
    notifier = FakeNotifier(
        [
            telegram_callback(50, 42, "manual:select:current_buy_up"),
            telegram_callback(51, 42, "manual:confirm:next_buy_down"),
        ]
    )
    poller = TelegramCommandPoller(notifier, "42", 50, Event())

    poller.poll_once()
    commands = poller.drain()

    assert [(item.command, item.argument) for item in commands] == [
        ("/manual_select", "current_buy_up"),
        ("/manual_confirm", "next_buy_down"),
    ]
