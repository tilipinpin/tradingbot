from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from src.telegram_commands import TelegramCommandPoller, reply_keyboard_markup


logger = logging.getLogger("telegram-notify")

BOT_TOKEN_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
PRIVATE_KEY_PATTERN = re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")
PROBABILITY_PATTERN = re.compile(r"\b(?:probability|p_up)=([01](?:\.\d+)?)")
MICRO_UNITS = Decimal("1000000")
POSITIONS_API = "https://data-api.polymarket.com/positions"


def _as_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _money(value: Decimal | None) -> str:
    return "N/A" if value is None else f"{value.quantize(Decimal('0.0001'))} pUSD"


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h {minutes}m {secs}s"


def sanitize_sensitive_text(value: Any) -> str:
    text = str(value)
    text = BOT_TOKEN_PATTERN.sub("<telegram-token-redacted>", text)
    return PRIVATE_KEY_PATTERN.sub("<private-key-redacted>", text)


def fill_amounts(order: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    response = order.get("response") if isinstance(order.get("response"), dict) else {}
    cost = _as_decimal(response.get("makingAmount"))
    shares = _as_decimal(response.get("takingAmount"))
    if cost <= 0:
        cost = _as_decimal(order.get("notional"))
    if shares <= 0:
        shares = _as_decimal(order.get("size"))
    price = cost / shares if shares > 0 else _as_decimal(order.get("price"))
    return cost, shares, price


def model_probability(order: dict[str, Any]) -> Decimal | None:
    match = PROBABILITY_PATTERN.search(str(order.get("reason") or ""))
    if match is None:
        return None
    probability = _as_decimal(match.group(1), "-1")
    if "p_up=" in match.group(0) and str(order.get("side", "")).upper() == "DOWN":
        probability = Decimal("1") - probability
    return probability if Decimal("0") <= probability <= Decimal("1") else None


def format_fill_message(order: dict[str, Any]) -> str:
    cost, shares, price = fill_amounts(order)
    winning_profit = shares - cost
    probability = model_probability(order)
    expected_profit = probability * shares - cost if probability is not None else None
    order_id = str((order.get("response") or {}).get("orderID") or "N/A")
    lines = [
        "💰 Polymarket 实际成交",
        f"市场: {order.get('slug', 'N/A')}",
        f"方向: {str(order.get('side', 'N/A')).upper()}",
        f"成交均价: {price.quantize(Decimal('0.0001'))}",
        f"数量: {shares.quantize(Decimal('0.0001'))} 份",
        f"实际投入: {_money(cost)}",
        f"获胜时毛收益: {_money(winning_profit)}",
    ]
    if expected_profit is not None:
        lines.append(f"模型期望收益: {_money(expected_profit)}（胜率 {probability:.2%}）")
    lines.append(f"订单: {order_id[:18]}{'…' if len(order_id) > 18 else ''}")
    return "\n".join(lines)


def settlement_key(order: dict[str, Any]) -> str:
    response = order.get("response") if isinstance(order.get("response"), dict) else {}
    order_id = str(response.get("orderID") or "").strip()
    if order_id:
        return order_id
    return ":".join(
        str(order.get(key) or "")
        for key in ("slug", "side", "matched_at", "price", "size")
    )


def settlement_values(order: dict[str, Any], winner: str) -> dict[str, Any]:
    cost, shares, price = fill_amounts(order)
    side = str(order.get("side") or "").upper()
    won = side == winner.upper()
    payout = shares if won else Decimal("0")
    gross_pnl = payout - cost
    return_rate = gross_pnl / cost if cost > 0 else None
    return {
        "slug": str(order.get("slug") or ""),
        "side": side,
        "winner": winner.upper(),
        "won": won,
        "cost": str(cost),
        "shares": str(shares),
        "entry_price": str(price),
        "payout": str(payout),
        "gross_pnl": str(gross_pnl),
        "return_rate": str(return_rate) if return_rate is not None else None,
    }


def format_settlement_message(
    settlement: dict[str, Any],
    balance: Decimal | None,
    all_settlements: list[dict[str, Any]],
) -> str:
    won = bool(settlement.get("won"))
    cost = _as_decimal(settlement.get("cost"))
    payout = _as_decimal(settlement.get("payout"))
    gross_pnl = _as_decimal(settlement.get("gross_pnl"))
    return_rate = settlement.get("return_rate")
    settled_count = len(all_settlements)
    wins = sum(1 for item in all_settlements if item.get("won") is True)
    cumulative_pnl = sum(
        (_as_decimal(item.get("gross_pnl")) for item in all_settlements),
        Decimal("0"),
    )
    win_rate = Decimal(wins) / Decimal(settled_count) if settled_count else Decimal("0")
    return_text = "N/A" if return_rate in (None, "") else f"{_as_decimal(return_rate):+.2%}"
    return "\n".join(
        [
            "🏁 Polymarket 交易已结算",
            f"市场: {settlement.get('slug', 'N/A')}",
            f"买入方向: {settlement.get('side', 'N/A')}",
            f"结算方向: {settlement.get('winner', 'N/A')}",
            f"结果: {'✅ 盈利' if won else '❌ 亏损'}",
            f"实际投入: {_money(cost)}",
            f"结算返还: {_money(payout)}",
            f"本单毛盈亏: {gross_pnl:+.4f} pUSD",
            f"本单收益率: {return_text}",
            f"当前钱包余额: {_money(balance)}",
            f"累计结算: {settled_count} 单（{wins} 胜 / {settled_count - wins} 负）",
            f"累计胜率: {win_rate:.2%}",
            f"累计毛盈亏: {cumulative_pnl:+.4f} pUSD",
            "注: 毛盈亏按成交投入和每份 1 pUSD 结算计算，钱包余额可能受手续费、赎回及转账影响。",
        ]
    )


@dataclass(frozen=True)
class DailyStats:
    fills: int
    settled: int
    wins: int
    losses: int
    unresolved: int
    gross_pnl: Decimal

    @property
    def win_rate(self) -> Decimal | None:
        if self.settled == 0:
            return None
        return Decimal(self.wins) / Decimal(self.settled)


def calculate_daily_stats(
    orders: list[dict[str, Any]],
    winners: dict[str, str | None],
) -> DailyStats:
    wins = 0
    losses = 0
    unresolved = 0
    gross_pnl = Decimal("0")
    for order in orders:
        winner = winners.get(str(order.get("slug") or ""))
        if winner not in {"UP", "DOWN"}:
            unresolved += 1
            continue
        cost, shares, _ = fill_amounts(order)
        if str(order.get("side") or "").upper() == winner:
            wins += 1
            gross_pnl += shares - cost
        else:
            losses += 1
            gross_pnl -= cost
    return DailyStats(
        fills=len(orders),
        settled=wins + losses,
        wins=wins,
        losses=losses,
        unresolved=unresolved,
        gross_pnl=gross_pnl,
    )


def format_daily_message(
    report_date: date,
    stats: DailyStats,
    start_balance: Decimal | None,
    end_balance: Decimal | None,
) -> str:
    balance_change = (
        end_balance - start_balance
        if start_balance is not None and end_balance is not None
        else None
    )
    estimated_costs = (
        stats.gross_pnl - balance_change
        if balance_change is not None and stats.unresolved == 0
        else None
    )
    win_rate = "N/A" if stats.win_rate is None else f"{stats.win_rate:.2%}"
    return "\n".join(
        [
            f"📊 Polymarket 日报 {report_date.isoformat()}",
            f"实际成交: {stats.fills} 单（已结算 {stats.settled}，待结算 {stats.unresolved}）",
            f"胜负: {stats.wins} 胜 / {stats.losses} 负",
            f"胜率: {win_rate}",
            f"策略毛盈亏: {_money(stats.gross_pnl)}",
            f"手续费/余额差额估算: {_money(estimated_costs)}",
            f"期初余额: {_money(start_balance)}",
            f"期末余额: {_money(end_balance)}",
            f"余额变化: {_money(balance_change)}",
            "注: 手续费为理论结算盈亏与钱包余额变化之差，充值、提现或未领取头寸会影响该估算。",
        ]
    )


class TelegramNotifier:
    def __init__(
        self,
        token: str | None,
        chat_id: str | None,
        timeout: float = 10,
        session: Any = requests,
    ) -> None:
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.timeout = timeout
        self.session = session
        self._last_alert_at: dict[str, float] = {}

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        enabled = os.getenv("TELEGRAM_ENABLED", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        token = os.getenv("TELEGRAM_BOT_TOKEN") if enabled else None
        chat_id = os.getenv("TELEGRAM_CHAT_ID") if enabled else None
        return cls(token, chat_id, timeout=float(os.getenv("TELEGRAM_TIMEOUT", "10")))

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, message: str, reply_markup: dict[str, Any] | None = None) -> bool:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": sanitize_sensitive_text(message),
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.api_request("sendMessage", payload) is not None

    def api_request(
        self,
        method: str,
        payload: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            response = self.session.post(
                f"https://api.telegram.org/bot{self.token}/{method}",
                json=payload,
                timeout=timeout if timeout is not None else self.timeout,
            )
            response.raise_for_status()
            response_payload = response.json()
            if response_payload.get("ok") is not True:
                raise RuntimeError("Telegram API returned ok=false")
            return response_payload
        except Exception as exc:
            logger.warning("Telegram API %s failed: %s", method, sanitize_sensitive_text(exc))
            return None

    def alert(self, key: str, message: str, cooldown: float = 300) -> bool:
        now = time.monotonic()
        previous = self._last_alert_at.get(key)
        if previous is not None and now - previous < cooldown:
            return False
        self._last_alert_at[key] = now
        return self.send(message)


class TradingNotificationService:
    def __init__(
        self,
        notifier: TelegramNotifier,
        trader: Any,
        signature_type: int,
        strategy: str,
        mode: str,
        version: str,
        summary: dict[str, Any],
        ledger_path: Path = Path("data/live_trade_events.jsonl"),
        state_path: Path = Path("data/telegram_daily_state.json"),
        timezone_name: str = "Asia/Shanghai",
        settlement_interval: float = 30,
        notify_on_matched: bool = False,
        commands_enabled: bool = False,
        wallet_address: str | None = None,
        positions_api: str = POSITIONS_API,
    ) -> None:
        self.notifier = notifier
        self.trader = trader
        self.signature_type = signature_type
        self.strategy = strategy
        self.mode = mode
        self.version = version
        self.summary = summary
        self.ledger_path = ledger_path
        self.state_path = state_path
        self.started_monotonic = time.monotonic()
        self.started_at = datetime.now(timezone.utc)
        self._stopped = False
        self._next_daily_attempt = 0.0
        self._next_settlement_attempt = 0.0
        self.settlement_interval = max(5, settlement_interval)
        self.notify_on_matched = notify_on_matched
        self.commands_enabled = commands_enabled
        self.wallet_address = (wallet_address or "").strip()
        self.positions_api = positions_api
        try:
            self.local_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            logger.warning("Unknown Telegram timezone %s; using Asia/Shanghai", timezone_name)
            self.local_timezone = ZoneInfo("Asia/Shanghai")
        self.state = self._load_state()
        self._pause_event = threading.Event()
        if bool(self.state.get("control", {}).get("paused")):
            self._pause_event.set()
        self._command_poller: TelegramCommandPoller | None = None
        self._saved_command_offset = int(self.state.get("telegram", {}).get("offset") or 0)
        self._runtime: dict[str, Any] = {
            "heartbeat": time.monotonic(),
            "slug": None,
            "seconds_left": None,
            "spot": None,
            "spot_source": None,
        }

    @classmethod
    def from_env(
        cls,
        trader: Any,
        signature_type: int,
        strategy: str,
        mode: str,
        version: str,
        summary: dict[str, Any],
        wallet_address: str | None = None,
    ) -> "TradingNotificationService":
        return cls(
            notifier=TelegramNotifier.from_env(),
            trader=trader,
            signature_type=signature_type,
            strategy=strategy,
            mode=mode,
            version=version,
            summary=summary,
            ledger_path=Path(os.getenv("TELEGRAM_TRADE_LEDGER", "data/live_trade_events.jsonl")),
            state_path=Path(os.getenv("TELEGRAM_DAILY_STATE", "data/telegram_daily_state.json")),
            timezone_name=os.getenv("TELEGRAM_TIMEZONE", "Asia/Shanghai"),
            settlement_interval=float(os.getenv("TELEGRAM_SETTLEMENT_INTERVAL", "30")),
            notify_on_matched=os.getenv("TELEGRAM_NOTIFY_ON_MATCHED", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            commands_enabled=os.getenv("TELEGRAM_COMMANDS_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            wallet_address=wallet_address,
            positions_api=os.getenv("POLYMARKET_POSITIONS_API", POSITIONS_API),
        )

    @property
    def enabled(self) -> bool:
        return self.notifier.enabled

    @property
    def trading_paused(self) -> bool:
        return self._pause_event.is_set()

    def update_runtime(
        self,
        slug: str | None = None,
        seconds_left: Decimal | None = None,
        spot: Decimal | None = None,
        spot_source: str | None = None,
    ) -> None:
        self._runtime["heartbeat"] = time.monotonic()
        if slug is not None:
            self._runtime["slug"] = slug
        if seconds_left is not None:
            self._runtime["seconds_left"] = seconds_left
        if spot is not None:
            self._runtime["spot"] = spot
        if spot_source is not None:
            self._runtime["spot_source"] = spot_source

    def current_balance(self) -> Decimal | None:
        if self.trader is None:
            return None
        try:
            try:
                from py_clob_client_v2 import AssetType, BalanceAllowanceParams
            except ImportError:
                from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=self.signature_type,
            )
            response = self.trader.client.get_balance_allowance(params)
            return _as_decimal(response.get("balance")) / MICRO_UNITS
        except Exception as exc:
            self.notify_exception("读取钱包余额", exc, key="balance-read")
            return None

    def start(self) -> None:
        if not self.enabled:
            logger.info("Telegram notifications disabled: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not configured")
            return
        balance = self.current_balance()
        local_now = datetime.now(self.local_timezone)
        self._ensure_day(local_now.date(), balance)
        self.notifier.send(
            "\n".join(
                [
                    "🚀 Polymarket 机器人启动成功",
                    f"版本: {self.version}",
                    f"服务器: {socket.gethostname()}",
                    f"模式: {self.mode}",
                    f"策略: {self.strategy}",
                    f"启动时间: {local_now.isoformat(timespec='seconds')}",
                    f"钱包余额: {_money(balance)}",
                ]
            ),
            reply_markup=reply_keyboard_markup(),
        )
        self._start_command_polling()

    def process_commands(self) -> bool:
        if self._command_poller is None:
            return False
        commands = self._command_poller.drain()
        drained_offset = self._command_poller.drained_offset

        restart_requested = False
        for item in commands:
            if item.command == "/balance":
                self._send_balance()
            elif item.command == "/pnl":
                self._send_today_pnl()
            elif item.command == "/positions":
                self._send_positions()
            elif item.command == "/status":
                self._send_status()
            elif item.command == "/stop":
                self._set_trading_paused(True)
                self._send_command_reply(
                    "⛔ 已暂停自动交易\n不会再提交新订单；已提交或已成交的订单不会被撤销。"
                )
            elif item.command == "/start":
                self._set_trading_paused(False)
                self._send_command_reply("▶️ 已恢复自动交易\n机器人将从下一次有效信号开始允许下单。")
            elif item.command == "/restart":
                self._send_command_reply("🔄 已收到重启指令\n正在保存状态并重启机器人。")
                restart_requested = True
        if drained_offset != self._saved_command_offset:
            self.state.setdefault("telegram", {})["offset"] = drained_offset
            self._saved_command_offset = drained_offset
            self._save_state()
        return restart_requested

    def prepare_restart(self) -> None:
        if self._command_poller is not None:
            self._command_poller.stop()
            self._command_poller = None

    def record_fill(self, order: dict[str, Any]) -> None:
        order["matched_at"] = order.get("matched_at") or datetime.now(timezone.utc).isoformat()
        if self.enabled:
            try:
                self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
                with self.ledger_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(order, ensure_ascii=False, default=str) + "\n")
            except OSError as exc:
                self.notify_exception("写入成交账本", exc, key="trade-ledger")
        if self.notify_on_matched:
            self.notifier.send(format_fill_message(order))

    def notify_exception(
        self,
        context: str,
        exc: Any,
        key: str | None = None,
        cooldown: float = 300,
    ) -> None:
        error = sanitize_sensitive_text(f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else exc)
        category = self._exception_category(error)
        self.notifier.alert(
            key or f"{category}:{context}",
            "\n".join(
                [
                    "⚠️ Polymarket 机器人异常",
                    f"类型: {category}",
                    f"环节: {context}",
                    f"详情: {error[:500]}",
                    f"时间: {datetime.now(self.local_timezone).isoformat(timespec='seconds')}",
                ]
            ),
            cooldown=cooldown,
        )

    def maybe_send_settlements(self, winner_lookup: Callable[[str], str | None]) -> None:
        now = time.monotonic()
        if not self.enabled or now < self._next_settlement_attempt:
            return
        self._next_settlement_attempt = now + self.settlement_interval
        settlements = self.state.setdefault("settlements", {})
        pending: list[dict[str, Any]] = []
        seen: set[str] = set()
        for order in self._ledger_orders():
            key = settlement_key(order)
            if not key or key in settlements or key in seen:
                continue
            seen.add(key)
            pending.append(order)
        if not pending:
            return

        winners: dict[str, str | None] = {}
        try:
            for slug in sorted({str(order.get("slug") or "") for order in pending}):
                winners[slug] = winner_lookup(slug)
        except Exception as exc:
            self.notify_exception("检查逐单结算", exc, key="settlement-check")
            return

        resolved = [
            (order, winners.get(str(order.get("slug") or "")))
            for order in pending
            if winners.get(str(order.get("slug") or "")) in {"UP", "DOWN"}
        ]
        if not resolved:
            return
        balance = self.current_balance()
        changed = False
        for order, winner in resolved:
            assert winner is not None
            key = settlement_key(order)
            record = settlement_values(order, winner)
            record["settled_at"] = datetime.now(timezone.utc).isoformat()
            cumulative = list(settlements.values()) + [record]
            if self.notifier.send(format_settlement_message(record, balance, cumulative)):
                settlements[key] = record
                changed = True
        if changed:
            self._save_state()

    def maybe_send_daily(self, winner_lookup: Callable[[str], str | None]) -> None:
        if not self.enabled or time.monotonic() < self._next_daily_attempt:
            return
        local_today = datetime.now(self.local_timezone).date()
        days = self.state.get("days", {})
        needs_roll = local_today.isoformat() not in days or any(
            day_text < local_today.isoformat() and "end_balance" not in item
            for day_text, item in days.items()
        )
        has_pending = any(
            day_text < local_today.isoformat() and not item.get("reported")
            for day_text, item in days.items()
        )
        if not needs_roll and not has_pending:
            return
        balance = self.current_balance()
        self._roll_days(local_today, balance)
        pending = sorted(
            day_text
            for day_text, item in self.state.get("days", {}).items()
            if day_text < local_today.isoformat() and not item.get("reported")
        )
        if not pending:
            return
        report_day = date.fromisoformat(pending[0])
        orders = self._orders_for_date(report_day)
        winners: dict[str, str | None] = {}
        try:
            for slug in sorted({str(order.get("slug") or "") for order in orders}):
                winners[slug] = winner_lookup(slug)
        except Exception as exc:
            self.notify_exception("生成每日报告", exc, key="daily-report")
            self._next_daily_attempt = time.monotonic() + 300
            return
        stats = calculate_daily_stats(orders, winners)
        day_state = self.state["days"][report_day.isoformat()]
        start_balance = self._optional_decimal(day_state.get("start_balance"))
        end_balance = self._optional_decimal(day_state.get("end_balance"))
        if self.notifier.send(format_daily_message(report_day, stats, start_balance, end_balance)):
            day_state["reported"] = True
            day_state["reported_at"] = datetime.now(timezone.utc).isoformat()
            self._save_state()

    def stop(self, reason: str, error: Any = None) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._command_poller is not None:
            self._command_poller.stop()
            self._command_poller = None
        if not self.enabled:
            return
        balance = self.current_balance()
        latest_error = error or self.summary.get("error")
        lines = [
            "🛑 Polymarket 机器人已停止",
            f"原因: {reason}",
            f"运行时长: {_duration(time.monotonic() - self.started_monotonic)}",
            f"累计尝试: {self.summary.get('order_attempts', 0)} 单",
            f"累计成交: {self.summary.get('matched_orders', 0)} 单",
            f"最终余额: {_money(balance)}",
        ]
        if latest_error:
            lines.append(f"最后错误: {sanitize_sensitive_text(latest_error)[:500]}")
        self.notifier.send("\n".join(lines))

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"days": {}, "settlements": {}, "control": {}, "telegram": {}}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("days"), dict):
                payload.setdefault("settlements", {})
                payload.setdefault("control", {})
                payload.setdefault("telegram", {})
                return payload
            return {"days": {}, "settlements": {}, "control": {}, "telegram": {}}
        except (OSError, json.JSONDecodeError):
            return {"days": {}, "settlements": {}, "control": {}, "telegram": {}}

    def _start_command_polling(self) -> None:
        if not self.commands_enabled or not self.enabled:
            return
        offset = self._saved_command_offset
        discard_pending = False
        if offset <= 0:
            payload = self.notifier.api_request(
                "getUpdates",
                {"offset": 0, "timeout": 0, "allowed_updates": ["message"]},
                timeout=self.notifier.timeout,
            )
            discard_pending = payload is None
            updates = payload.get("result") if payload is not None else []
            update_ids = [
                int(update.get("update_id") or 0)
                for update in (updates or [])
                if isinstance(update, dict)
            ]
            if update_ids:
                offset = max(update_ids) + 1
                self.state.setdefault("telegram", {})["offset"] = offset
                self._saved_command_offset = offset
                self._save_state()
        self.notifier.api_request(
            "setMyCommands",
            {
                "commands": [
                    {"command": "balance", "description": "查看钱包余额"},
                    {"command": "pnl", "description": "查看今日盈亏"},
                    {"command": "positions", "description": "查看当前持仓"},
                    {"command": "status", "description": "查看机器人状态"},
                    {"command": "stop", "description": "紧急暂停新交易"},
                    {"command": "start", "description": "恢复自动交易"},
                    {"command": "restart", "description": "重启机器人"},
                ]
            },
        )
        self.notifier.api_request(
            "setChatMenuButton",
            {
                "chat_id": self.notifier.chat_id,
                "menu_button": {"type": "commands"},
            },
        )
        self._command_poller = TelegramCommandPoller(
            notifier=self.notifier,
            allowed_chat_id=self.notifier.chat_id,
            offset=offset,
            pause_event=self._pause_event,
            discard_pending=discard_pending,
        )
        self._command_poller.start()

    def _set_trading_paused(self, paused: bool) -> None:
        if paused:
            self._pause_event.set()
        else:
            self._pause_event.clear()
        self.state.setdefault("control", {})["paused"] = paused
        self.state["control"]["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_state()

    def _send_balance(self) -> None:
        balance = self.current_balance()
        self._send_command_reply(
            "\n".join(
                [
                    "📈 Polymarket 钱包余额",
                    f"可用抵押余额: {_money(balance)}",
                    f"查询时间: {datetime.now(self.local_timezone).isoformat(timespec='seconds')}",
                ]
            )
        )

    def _send_today_pnl(self) -> None:
        today = datetime.now(self.local_timezone).date()
        orders = self._orders_for_date(today)
        settlements = self.state.get("settlements", {})
        resolved = [
            settlements[settlement_key(order)]
            for order in orders
            if settlement_key(order) in settlements
        ]
        wins = sum(1 for item in resolved if item.get("won") is True)
        gross_pnl = sum(
            (_as_decimal(item.get("gross_pnl")) for item in resolved),
            Decimal("0"),
        )
        settled = len(resolved)
        win_rate = "N/A" if settled == 0 else f"{Decimal(wins) / Decimal(settled):.2%}"
        self._send_command_reply(
            "\n".join(
                [
                    f"📊 今日盈亏 {today.isoformat()}",
                    f"实际成交: {len(orders)} 单",
                    f"已结算: {settled} 单，待结算: {len(orders) - settled} 单",
                    f"胜负: {wins} 胜 / {settled - wins} 负",
                    f"胜率: {win_rate}",
                    f"已结算毛盈亏: {gross_pnl:+.4f} pUSD",
                    f"当前钱包余额: {_money(self.current_balance())}",
                ]
            )
        )

    def _send_positions(self) -> None:
        if not self.wallet_address:
            self._send_command_reply("📋 无法查询持仓\n未配置 DEPOSIT_WALLET/FUNDER_ADDRESS。")
            return
        try:
            response = requests.get(
                self.positions_api,
                params={
                    "user": self.wallet_address,
                    "sizeThreshold": "0.01",
                    "limit": "100",
                },
                timeout=self.notifier.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            positions = payload if isinstance(payload, list) else []
        except Exception as exc:
            self.notify_exception("查询持仓", exc, key="positions-read", cooldown=60)
            self._send_command_reply("📋 持仓查询失败\n请检查 Data API、代理和网络连接。")
            return
        if not positions:
            self._send_command_reply("📋 当前持仓\n暂无大于 0.01 份的持仓。")
            return

        total_value = sum(
            (_as_decimal(item.get("currentValue")) for item in positions),
            Decimal("0"),
        )
        total_pnl = sum(
            (_as_decimal(item.get("cashPnl")) for item in positions),
            Decimal("0"),
        )
        lines = [
            f"📋 当前持仓（{len(positions)} 项）",
            f"当前总价值: {_money(total_value)}",
            f"持仓现金盈亏: {total_pnl:+.4f} pUSD",
        ]
        for index, item in enumerate(positions[:10], start=1):
            title = str(item.get("title") or item.get("slug") or "N/A")
            if len(title) > 45:
                title = title[:42] + "..."
            lines.extend(
                [
                    "",
                    f"{index}. {title}",
                    f"{str(item.get('outcome') or 'N/A').upper()} | "
                    f"{_as_decimal(item.get('size')):.4f} 份 | "
                    f"均价 {_as_decimal(item.get('avgPrice')):.4f} | "
                    f"现价 {_as_decimal(item.get('curPrice')):.4f}",
                    f"价值 {_as_decimal(item.get('currentValue')):.4f} pUSD | "
                    f"盈亏 {_as_decimal(item.get('cashPnl')):+.4f} pUSD"
                    f"{' | 可赎回' if item.get('redeemable') else ''}",
                ]
            )
        if len(positions) > 10:
            lines.append(f"\n另有 {len(positions) - 10} 项未展开。")
        self._send_command_reply("\n".join(lines))

    def _send_status(self) -> None:
        heartbeat_age = max(0, int(time.monotonic() - self._runtime["heartbeat"]))
        seconds_left = self._runtime.get("seconds_left")
        spot = self._runtime.get("spot")
        lines = [
            "❤️ Polymarket 机器人状态",
            "进程: 正常运行",
            f"交易: {'已暂停' if self.trading_paused else '运行中'}",
            f"版本: {self.version}",
            f"模式: {self.mode}",
            f"策略: {self.strategy}",
            f"运行时长: {_duration(time.monotonic() - self.started_monotonic)}",
            f"心跳: {heartbeat_age} 秒前",
            f"累计尝试/成交: {self.summary.get('order_attempts', 0)}/{self.summary.get('matched_orders', 0)}",
        ]
        if self._runtime.get("slug"):
            lines.append(f"当前市场: {self._runtime['slug']}")
        if seconds_left is not None:
            lines.append(f"窗口剩余: {max(0, int(_as_decimal(seconds_left)))} 秒")
        if spot is not None:
            lines.append(
                f"BTC/USD: {_as_decimal(spot):.2f}（{self._runtime.get('spot_source') or 'N/A'}）"
            )
        self._send_command_reply("\n".join(lines))

    def _send_command_reply(self, message: str) -> bool:
        return self.notifier.send(message, reply_markup=reply_keyboard_markup())

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _ensure_day(self, day: date, balance: Decimal | None) -> None:
        days = self.state.setdefault("days", {})
        day_state = days.setdefault(day.isoformat(), {"reported": False})
        if "start_balance" not in day_state:
            day_state["start_balance"] = str(balance) if balance is not None else None
            day_state["started_at"] = datetime.now(timezone.utc).isoformat()
            self._save_state()

    def _roll_days(self, local_today: date, balance: Decimal | None) -> None:
        days = self.state.setdefault("days", {})
        changed = False
        for day_text, day_state in days.items():
            if day_text < local_today.isoformat() and "end_balance" not in day_state:
                day_state["end_balance"] = str(balance) if balance is not None else None
                changed = True
        if local_today.isoformat() not in days:
            days[local_today.isoformat()] = {
                "reported": False,
                "start_balance": str(balance) if balance is not None else None,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            changed = True
        if changed:
            self._save_state()

    def _orders_for_date(self, report_date: date) -> list[dict[str, Any]]:
        orders: list[dict[str, Any]] = []
        for order in self._ledger_orders():
            try:
                matched_at = datetime.fromisoformat(str(order["matched_at"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            if matched_at.astimezone(self.local_timezone).date() == report_date:
                orders.append(order)
        return orders

    def _ledger_orders(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        orders: list[dict[str, Any]] = []
        try:
            lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            self.notify_exception("读取成交账本", exc, key="trade-ledger-read")
            return []
        for line in lines:
            try:
                order = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(order, dict):
                orders.append(order)
        return orders

    @staticmethod
    def _optional_decimal(value: Any) -> Decimal | None:
        return None if value in (None, "") else _as_decimal(value)

    @staticmethod
    def _exception_category(error: str) -> str:
        lowered = error.lower()
        if any(word in lowered for word in ("sign", "signature", "签名")):
            return "签名失败"
        if any(word in lowered for word in ("insufficient", "balance", "allowance", "余额")):
            return "余额或授权不足"
        if any(word in lowered for word in ("timeout", "timed out", "超时")):
            return "RPC/API 超时"
        if any(word in lowered for word in ("network", "connection", "proxy", "dns", "网络")):
            return "网络中断"
        if "rpc" in lowered:
            return "RPC 异常"
        return "交易程序异常"
