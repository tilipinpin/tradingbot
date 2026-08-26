from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any


logger = logging.getLogger("telegram-commands")

SUPPORTED_COMMANDS = {
    "/balance",
    "/pnl",
    "/positions",
    "/status",
    "/strategy",
    "/manual",
    "/stop",
    "/start",
    "/restart",
}
INTERNAL_CALLBACK_COMMANDS = {
    "/strategy_select",
    "/strategy_confirm",
    "/strategy_unavailable",
    "/strategy_cancel",
    "/manual_select",
    "/manual_confirm",
    "/manual_cancel",
}
MANUAL_ACTIONS = frozenset(
    {
        "current_buy_up",
        "current_buy_down",
        "current_sell_up",
        "current_sell_down",
        "next_buy_up",
        "next_buy_down",
    }
)
STRATEGY_LABELS = {
    "fair_value_edge": "公允价值差",
    "fast_directional_hedge_simple": "盘口波动套利",
    "smart_score": "智能评分",
    "ewma_twap_fair": "EWMA·TWAP公平价",
    "momentum_confirmation": "动量确认",
    "reversal_first_stage": "反转·首段",
    "reversal_three_16": "3窗反转·16U",
    "reversal_v11": "5窗反转·10阶",
    "reversal_v11_four_streak": "4窗反转·10阶",
    "reversal_v11_six_streak": "4窗反转·4-0-0追回",
    "reversal_four_64": "4窗反转·64U",
    "reversal_three_4_8": "2窗反转·12阶",
}
PAPER_ONLY_STRATEGIES: set[str] = set()
LIVE_STRATEGIES = {
    "fair_value_edge",
    "fast_directional_hedge_simple",
    "smart_score",
    "ewma_twap_fair",
    "reversal_four_64",
    "momentum_confirmation",
    "reversal_v11",
    "reversal_v11_four_streak",
    "reversal_v11_six_streak",
    "reversal_first_stage",
    "reversal_three_16",
    "reversal_three_4_8",
}
REVERSAL_TRIGGER_STREAKS = {
    "reversal_four_64": 4,
    "reversal_v11": 5,
    "reversal_v11_four_streak": 4,
    "reversal_v11_six_streak": 4,
    "reversal_first_stage": 4,
    "reversal_three_16": 3,
    "reversal_three_4_8": 2,
}
REVERSAL_STRATEGIES = frozenset(REVERSAL_TRIGGER_STREAKS)
REVERSAL_STAGE_LIMITS = {
    "reversal_v11": 10,
    "reversal_v11_four_streak": 10,
    "reversal_v11_six_streak": 10,
    "reversal_first_stage": 1,
    "reversal_three_16": 25,
    "reversal_three_4_8": 12,
}
EXECUTION_ADAPTER_PENDING_STRATEGIES: set[str] = set()
DEFAULT_LAUNCH_STRATEGY = "reversal_three_16"
DEFAULT_STRATEGY = "__default__"
BUTTON_COMMANDS = {
    "📈 查看余额": "/balance",
    "📊 今日盈亏": "/pnl",
    "📋 查看持仓": "/positions",
    "❤️ 运行状态": "/status",
    "🧠 选择策略": "/strategy",
    "🎛 手动交易": "/manual",
    "⛔ 停止交易": "/stop",
    "▶️ 恢复交易": "/start",
}


def reply_keyboard_markup() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "📈 查看余额"}, {"text": "📊 今日盈亏"}],
            [{"text": "📋 查看持仓"}, {"text": "❤️ 运行状态"}],
            [{"text": "⛔ 停止交易"}, {"text": "▶️ 恢复交易"}],
            [{"text": "🧠 选择策略"}, {"text": "🎛 手动交易"}],
        ],
        "is_persistent": True,
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "选择机器人操作",
    }


def strategy_selection_markup(
    mode: str,
    active_strategy: str,
    pending_strategy: str | None,
) -> dict[str, Any]:
    buttons: list[dict[str, str]] = []
    for strategy, label in STRATEGY_LABELS.items():
        adapter_pending = strategy in EXECUTION_ADAPTER_PENDING_STRATEGIES
        unavailable = adapter_pending or (mode == "live" and strategy not in LIVE_STRATEGIES)
        prefix = "✅ " if strategy == active_strategy else "⏳ " if strategy == pending_strategy else ""
        suffix = (
            "（链上配置待完成）"
            if adapter_pending
            else "（仅纸面）"
            if unavailable and strategy in PAPER_ONLY_STRATEGIES
            else "（实盘待验证）"
            if unavailable
            else ""
        )
        action = "unavailable" if unavailable else "select"
        buttons.append(
            {
                "text": f"{prefix}{label}{suffix}",
                "callback_data": f"strategy:{action}:{strategy}",
            }
        )
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    rows.extend(
        [
            [{"text": "↩️ 恢复启动策略", "callback_data": f"strategy:select:{DEFAULT_STRATEGY}"}],
            [{"text": "取消", "callback_data": "strategy:cancel"}],
        ]
    )
    return {"inline_keyboard": rows}


def strategy_confirmation_markup(strategy: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "确认下个窗口切换", "callback_data": f"strategy:confirm:{strategy}"},
                {"text": "取消", "callback_data": "strategy:cancel"},
            ]
        ]
    }


def manual_trade_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "本窗口 买UP 2份", "callback_data": "manual:select:current_buy_up"},
                {"text": "本窗口 买DOWN 2份", "callback_data": "manual:select:current_buy_down"},
            ],
            [
                {"text": "本窗口 卖UP全部", "callback_data": "manual:select:current_sell_up"},
                {"text": "本窗口 卖DOWN全部", "callback_data": "manual:select:current_sell_down"},
            ],
            [
                {"text": "下一窗口 买UP 2份", "callback_data": "manual:select:next_buy_up"},
                {"text": "下一窗口 买DOWN 2份", "callback_data": "manual:select:next_buy_down"},
            ],
            [{"text": "取消", "callback_data": "manual:cancel"}],
        ]
    }


def manual_confirmation_markup(action: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "确认提交", "callback_data": f"manual:confirm:{action}"},
                {"text": "取消", "callback_data": "manual:cancel"},
            ]
        ]
    }


@dataclass(frozen=True)
class TelegramCommand:
    update_id: int
    command: str
    argument: str | None = None
    callback_query_id: str | None = None


class TelegramCommandPoller:
    def __init__(
        self,
        notifier: Any,
        allowed_chat_id: str,
        offset: int,
        pause_event: Event,
        poll_timeout: int = 20,
        discard_pending: bool = False,
    ) -> None:
        self.notifier = notifier
        self.allowed_chat_id = str(allowed_chat_id)
        self.offset = max(0, int(offset))
        self.pause_event = pause_event
        self.poll_timeout = max(1, poll_timeout)
        self.discard_pending = discard_pending
        self._commands: Queue[TelegramCommand] = Queue()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self.drained_offset = self.offset

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = Thread(target=self._run, name="telegram-command-poller", daemon=True)
        self._thread.start()

    def stop(self, wait: bool = True) -> None:
        self._stop_event.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=2)

    def drain(self) -> list[TelegramCommand]:
        commands: list[TelegramCommand] = []
        with self._lock:
            while True:
                try:
                    commands.append(self._commands.get_nowait())
                except Empty:
                    self.drained_offset = self.offset
                    return commands

    def poll_once(self) -> bool:
        payload = self.notifier.api_request(
            "getUpdates",
            {
                "offset": self.offset,
                "timeout": self.poll_timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=self.poll_timeout + 5,
        )
        if payload is None:
            return False
        updates = payload.get("result") or []
        with self._lock:
            discard_updates = self.discard_pending
            for update in updates:
                if not isinstance(update, dict):
                    continue
                update_id = int(update.get("update_id") or 0)
                self.offset = max(self.offset, update_id + 1)
                if discard_updates:
                    continue
                callback_query = update.get("callback_query") or {}
                message = update.get("message") or callback_query.get("message") or {}
                chat = message.get("chat") or {}
                if str(chat.get("id")) != self.allowed_chat_id:
                    continue
                argument = None
                callback_query_id = None
                if callback_query:
                    data = str(callback_query.get("data") or "")
                    parts = data.split(":", 2)
                    if len(parts) < 2 or parts[0] not in {"strategy", "manual"}:
                        continue
                    action = parts[1]
                    argument = parts[2] if len(parts) == 3 else None
                    namespace = parts[0]
                    command = f"/{namespace}_{action}"
                    callback_query_id = str(callback_query.get("id") or "") or None
                    if command not in INTERNAL_CALLBACK_COMMANDS:
                        continue
                    if namespace == "strategy":
                        if command != "/strategy_cancel" and argument not in {
                            *STRATEGY_LABELS,
                            DEFAULT_STRATEGY,
                        }:
                            continue
                    elif command != "/manual_cancel" and argument not in MANUAL_ACTIONS:
                        continue
                else:
                    text = str(message.get("text") or "").strip()
                    if text.startswith("/"):
                        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
                    else:
                        command = BUTTON_COMMANDS.get(text, "")
                    if command not in SUPPORTED_COMMANDS:
                        continue
                if command in {"/stop", "/restart"}:
                    self.pause_event.set()
                elif command == "/start":
                    self.pause_event.clear()
                self._commands.put(
                    TelegramCommand(
                        update_id=update_id,
                        command=command,
                        argument=argument,
                        callback_query_id=callback_query_id,
                    )
                )
            self.discard_pending = False
        return True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if not self.poll_once():
                self._stop_event.wait(2)
            else:
                time.sleep(0.05)
