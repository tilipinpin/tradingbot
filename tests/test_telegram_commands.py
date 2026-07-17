from __future__ import annotations

from threading import Event

from src.telegram_commands import (
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
        "🔄 重启机器人",
    ]


def test_poller_maps_large_keyboard_button_to_command() -> None:
    notifier = FakeNotifier([telegram_update(30, 42, "📋 查看持仓")])
    poller = TelegramCommandPoller(notifier, "42", 30, Event())

    poller.poll_once()

    assert [item.command for item in poller.drain()] == ["/positions"]


def test_live_strategy_menu_marks_paper_strategies_unavailable() -> None:
    markup = strategy_selection_markup("live", "fair_value_edge", "late_favorite")
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    by_data = {button["callback_data"]: button["text"] for button in buttons}

    assert "✅ 公允价值差" == by_data["strategy:select:fair_value_edge"]
    assert "尾盘高置信度（仅纸面）" in by_data["strategy:unavailable:late_favorite"]
    assert len(by_data) == 6


def test_poller_parses_strategy_callback_from_authorized_chat() -> None:
    notifier = FakeNotifier([telegram_callback(40, 42, "strategy:select:late_favorite")])
    poller = TelegramCommandPoller(notifier, "42", 40, Event())

    poller.poll_once()
    commands = poller.drain()

    assert len(commands) == 1
    assert commands[0].command == "/strategy_select"
    assert commands[0].argument == "late_favorite"
    assert commands[0].callback_query_id == "callback-40"
