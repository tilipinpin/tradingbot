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
    "/stop",
    "/start",
    "/restart",
}
BUTTON_COMMANDS = {
    "📈 查看余额": "/balance",
    "📊 今日盈亏": "/pnl",
    "📋 查看持仓": "/positions",
    "❤️ 运行状态": "/status",
    "⛔ 停止交易": "/stop",
    "▶️ 恢复交易": "/start",
    "🔄 重启机器人": "/restart",
}


def reply_keyboard_markup() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "📈 查看余额"}, {"text": "📊 今日盈亏"}],
            [{"text": "📋 查看持仓"}, {"text": "❤️ 运行状态"}],
            [{"text": "⛔ 停止交易"}, {"text": "▶️ 恢复交易"}],
            [{"text": "🔄 重启机器人"}],
        ],
        "is_persistent": True,
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "选择机器人操作",
    }


@dataclass(frozen=True)
class TelegramCommand:
    update_id: int
    command: str


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
                "allowed_updates": ["message"],
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
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                if str(chat.get("id")) != self.allowed_chat_id:
                    continue
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
                self._commands.put(TelegramCommand(update_id=update_id, command=command))
            self.discard_pending = False
        return True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if not self.poll_once():
                self._stop_event.wait(2)
            else:
                time.sleep(0.05)
