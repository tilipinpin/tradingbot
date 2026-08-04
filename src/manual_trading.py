from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any, Callable, Protocol

from src.polymarket import Market, OrderBookSnapshot


logger = logging.getLogger("manual-trading")

MANUAL_BUY_SHARES = Decimal("2")
SELL_SIZE_QUANTUM = Decimal("0.01")


class ManualTrader(Protocol):
    def collateral_balance(self, signature_type: int) -> Decimal: ...

    def conditional_balance(self, token_id: str, signature_type: int) -> Decimal: ...

    def buy_limit(
        self,
        token_id: str,
        price: Decimal,
        size: Decimal,
        tick_size: str,
        neg_risk: bool,
        order_type: str = "GTC",
        submit_not_after_monotonic: float | None = None,
        post_only: bool = False,
    ) -> dict[str, Any]: ...

    def sell_limit(
        self,
        token_id: str,
        price: Decimal,
        size: Decimal,
        tick_size: str,
        neg_risk: bool,
        order_type: str = "GTC",
        submit_not_after_monotonic: float | None = None,
        post_only: bool = False,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ManualTradeRequest:
    request_id: str
    target_slug: str
    action: str
    side: str
    requested_at: str
    sell_size: Decimal | None = None

    def __post_init__(self) -> None:
        if self.action not in {"buy", "sell"}:
            raise ValueError("manual action must be buy or sell")
        if self.side not in {"UP", "DOWN"}:
            raise ValueError("manual side must be UP or DOWN")
        if self.action == "sell" and (self.sell_size is None or self.sell_size <= 0):
            raise ValueError("manual sell requires a positive reserved size")


@dataclass(frozen=True)
class ManualTradeResult:
    request: ManualTradeRequest
    status: str
    price: Decimal | None = None
    requested_size: Decimal = Decimal("0")
    filled_size: Decimal = Decimal("0")
    cash_amount: Decimal = Decimal("0")
    remaining_size: Decimal = Decimal("0")
    response: dict[str, Any] | None = None
    error: str | None = None

    def ledger_record(self) -> dict[str, Any]:
        return {
            **asdict(self.request),
            "order_role": f"manual_{self.request.action}",
            "price": str(self.price) if self.price is not None else None,
            "size": str(self.requested_size),
            "filled_size": str(self.filled_size),
            "notional": str(self.cash_amount),
            "remaining_size": str(self.remaining_size),
            "order_type": "FAK",
            "manual": True,
            "response": self.response,
            "error": self.error,
        }


def next_window_slug(slug: str) -> str:
    prefix, separator, epoch_text = slug.rpartition("-")
    if not separator:
        raise ValueError("market slug has no epoch suffix")
    return f"{prefix}-{int(epoch_text) + 300}"


def slug_epoch(slug: str) -> int:
    try:
        return int(slug.rpartition("-")[2])
    except ValueError as exc:
        raise ValueError("market slug has an invalid epoch suffix") from exc


def _fill_amounts(
    response: dict[str, Any],
    action: str,
) -> tuple[Decimal, Decimal]:
    if not isinstance(response, dict) or str(response.get("status", "")).lower() != "matched":
        return Decimal("0"), Decimal("0")
    if action == "buy":
        cash = Decimal(str(response.get("makingAmount") or "0"))
        shares = Decimal(str(response.get("takingAmount") or "0"))
    else:
        shares = Decimal(str(response.get("makingAmount") or "0"))
        cash = Decimal(str(response.get("takingAmount") or "0"))
    return max(Decimal("0"), shares), max(Decimal("0"), cash)


class ManualTradeExecutor:
    """Execute Telegram manual orders off the automatic strategy thread."""

    def __init__(
        self,
        *,
        trader: ManualTrader,
        signature_type: int,
        market_loader: Callable[[str], Market],
        book_loader: Callable[[tuple[str, str]], tuple[OrderBookSnapshot, OrderBookSnapshot]],
        on_submitting: Callable[[ManualTradeRequest], None],
        on_result: Callable[[ManualTradeResult], None],
        buy_slippage: Decimal = Decimal("0.03"),
        sell_slippage: Decimal = Decimal("0.03"),
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        if buy_slippage < 0 or sell_slippage < 0:
            raise ValueError("manual slippage must not be negative")
        self.trader = trader
        self.signature_type = signature_type
        self.market_loader = market_loader
        self.book_loader = book_loader
        self.on_submitting = on_submitting
        self.on_result = on_result
        self.buy_slippage = buy_slippage
        self.sell_slippage = sell_slippage
        self.time_fn = time_fn
        self._incoming: Queue[ManualTradeRequest] = Queue()
        self._pending: dict[str, ManualTradeRequest] = {}
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = Thread(target=self._run, name="manual-trade-executor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def submit(self, request: ManualTradeRequest) -> None:
        self._incoming.put(request)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                request = self._incoming.get(timeout=0.25)
                self._pending.setdefault(request.request_id, request)
            except Empty:
                pass
            while True:
                try:
                    request = self._incoming.get_nowait()
                    self._pending.setdefault(request.request_id, request)
                except Empty:
                    break
            now = self.time_fn()
            ready = sorted(
                (
                    request
                    for request in self._pending.values()
                    if slug_epoch(request.target_slug) <= now
                ),
                key=lambda request: (slug_epoch(request.target_slug), request.request_id),
            )
            for request in ready:
                self._pending.pop(request.request_id, None)
                self.on_result(self.execute(request))

    def execute(self, request: ManualTradeRequest) -> ManualTradeResult:
        start_epoch = slug_epoch(request.target_slug)
        now = self.time_fn()
        if now >= start_epoch + 300:
            return ManualTradeResult(
                request=request,
                status="expired",
                error="目标五分钟窗口已经结束",
            )
        try:
            market = self.market_loader(request.target_slug)
            if market.event_start_time is not None and now < market.event_start_time.timestamp():
                return ManualTradeResult(
                    request=request,
                    status="not_ready",
                    error="目标窗口尚未开始",
                )
            up_book, down_book = self.book_loader(market.token_ids)
            book = up_book if request.side == "UP" else down_book
            token_id = market.token_ids[0 if request.side == "UP" else 1]
            self.on_submitting(request)
            if request.action == "buy":
                ask = book.quote.ask
                if ask is None or ask <= 0 or ask >= 1:
                    raise RuntimeError("所选方向当前没有可成交卖价")
                tick = Decimal(market.minimum_tick_size)
                limit_price = min(Decimal("1") - tick, ask + self.buy_slippage)
                size = MANUAL_BUY_SHARES
                required = limit_price * size
                if self.trader.collateral_balance(self.signature_type) < required:
                    raise RuntimeError(f"可用余额不足，需要最多 {required} pUSD")
                response = self.trader.buy_limit(
                    token_id,
                    limit_price,
                    size,
                    market.minimum_tick_size,
                    market.neg_risk,
                    "FAK",
                    submit_not_after_monotonic=time.monotonic() + 1.0,
                )
                filled, cash = _fill_amounts(response, "buy")
                return ManualTradeResult(
                    request=request,
                    status="matched" if filled > 0 else "unmatched",
                    price=limit_price,
                    requested_size=size,
                    filled_size=filled,
                    cash_amount=cash,
                    remaining_size=max(Decimal("0"), size - filled),
                    response=response,
                )

            available = self.trader.conditional_balance(token_id, self.signature_type)
            assert request.sell_size is not None
            size = min(available, request.sell_size).quantize(
                SELL_SIZE_QUANTUM,
                rounding=ROUND_DOWN,
            )
            if size <= 0:
                raise RuntimeError("所选方向没有可卖出的持仓")
            bid = book.quote.bid
            if bid is None or bid <= 0:
                raise RuntimeError("所选方向当前没有可成交买价")
            tick = Decimal(market.minimum_tick_size)
            limit_price = max(tick, bid - self.sell_slippage)
            response = self.trader.sell_limit(
                token_id,
                limit_price,
                size,
                market.minimum_tick_size,
                market.neg_risk,
                "FAK",
                submit_not_after_monotonic=time.monotonic() + 1.0,
            )
            filled, cash = _fill_amounts(response, "sell")
            remaining = max(Decimal("0"), available - filled)
            return ManualTradeResult(
                request=request,
                status="matched" if filled > 0 else "unmatched",
                price=limit_price,
                requested_size=size,
                filled_size=filled,
                cash_amount=cash,
                remaining_size=remaining,
                response=response,
            )
        except Exception as exc:
            logger.warning(
                "MANUAL_ORDER_FAILED request=%s slug=%s action=%s side=%s error=%s",
                request.request_id,
                request.target_slug,
                request.action,
                request.side,
                exc,
            )
            return ManualTradeResult(
                request=request,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
