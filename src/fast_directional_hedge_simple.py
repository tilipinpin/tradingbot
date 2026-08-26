from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from src.polymarket import OrderBookSnapshot


ZERO = Decimal("0")
ONE = Decimal("1")


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _best_ask(book: OrderBookSnapshot) -> Decimal | None:
    return min((level.price for level in book.asks if level.price > 0 and level.size > 0), default=None)


def _vwap(book: OrderBookSnapshot, quantity: Decimal, *, buy: bool) -> tuple[Decimal, Decimal, Decimal] | None:
    if quantity <= 0:
        return None
    levels = sorted(
        book.asks if buy else book.bids,
        key=lambda level: level.price,
        reverse=not buy,
    )
    remaining = quantity
    value = ZERO
    filled = ZERO
    worst = ZERO if buy else ONE
    for level in levels:
        if level.price <= 0 or level.size <= 0:
            continue
        take = min(remaining, level.size)
        value += take * level.price
        filled += take
        remaining -= take
        worst = max(worst, level.price) if buy else min(worst, level.price)
        if remaining <= 0:
            break
    if filled <= 0:
        return None
    return value / filled, worst, filled


@dataclass(frozen=True)
class FastDirectionalHedgeSimpleSettings:
    strategy_id: str = "fast_directional_hedge_simple"
    version: str = "2.0"
    enabled: bool = False
    entry_price_min: Decimal = Decimal("0.53")
    entry_price_max: Decimal = Decimal("0.60")
    entry_confirm_ticks: int = 2
    entry_confirm_min_interval_ms: int = 150
    base_position_size: Decimal = Decimal("2")
    min_ask_gap: Decimal = Decimal("0.04")
    max_spread: Decimal = Decimal("0.04")
    max_ask_sum: Decimal = Decimal("1.06")
    max_entry_drift: Decimal = Decimal("0.02")
    entry_max_slippage: Decimal = Decimal("0.02")
    initial_stop_pct: Decimal = Decimal("0.25")
    trailing_enabled: bool = True
    trailing_start_gain: Decimal = Decimal("0.15")
    break_even_enabled: bool = True
    break_even_buffer: Decimal = Decimal("0.00")
    trailing_drawdown_pct: Decimal = Decimal("0.20")
    stop_confirm_ticks: int = 2
    fast_move_window_ms: int = 500
    fast_move_threshold: Decimal = Decimal("0.05")
    fast_stop_confirm_ticks: int = 1
    emergency_stop_penetration: Decimal = Decimal("0.06")
    prefer_hedge: bool = True
    hedge_max_slippage: Decimal = Decimal("0.05")
    hedge_max_price: Decimal = Decimal("0.85")
    hedge_entry_max_seconds: Decimal = Decimal("150")
    hedge_entry_min_seconds: Decimal = Decimal("30")
    exit_max_slippage: Decimal = Decimal("0.03")
    take_profit_net_per_share: Decimal = Decimal("0.02")
    take_profit_confirm_ticks: int = 1
    hedge_until_fully_covered: bool = True
    max_entries_per_window: int = 2
    normal_entry_max_seconds: Decimal = Decimal("180")
    normal_entry_min_seconds: Decimal = Decimal("60")
    stop_new_entry_time: Decimal = Decimal("30")
    risk_only_time: Decimal = Decimal("15")
    max_book_age_seconds: Decimal = Decimal("0.50")
    fee_rate: Decimal = Decimal("0.07")

    def __post_init__(self) -> None:
        if not ZERO < self.entry_price_min <= self.entry_price_max < ONE:
            raise ValueError("entry price range must be within (0, 1)")
        if (
            self.entry_confirm_ticks < 1
            or self.stop_confirm_ticks < 1
            or self.fast_stop_confirm_ticks < 1
            or self.take_profit_confirm_ticks < 1
        ):
            raise ValueError("confirmation ticks must be positive")
        if self.entry_confirm_min_interval_ms < 0:
            raise ValueError("entry confirmation interval cannot be negative")
        if self.base_position_size <= 0 or self.max_entries_per_window < 1:
            raise ValueError("position size and entry limit must be positive")
        if self.normal_entry_min_seconds > self.normal_entry_max_seconds:
            raise ValueError("entry time range is invalid")
        if self.hedge_entry_min_seconds > self.hedge_entry_max_seconds:
            raise ValueError("hedge time range is invalid")
        if not ZERO < self.hedge_max_price < ONE:
            raise ValueError("hedge max price must be within (0, 1)")
        if not ZERO < self.initial_stop_pct < ONE or not ZERO < self.trailing_drawdown_pct < ONE:
            raise ValueError("stop percentages must be within (0, 1)")
        if min(
            self.max_entry_drift,
            self.entry_max_slippage,
            self.min_ask_gap,
            self.max_spread,
            self.max_ask_sum,
            self.trailing_start_gain,
            self.break_even_buffer,
            self.fast_move_threshold,
            self.emergency_stop_penetration,
            self.hedge_max_slippage,
            self.hedge_max_price,
            self.hedge_entry_max_seconds,
            self.hedge_entry_min_seconds,
            self.exit_max_slippage,
            self.take_profit_net_per_share,
            self.normal_entry_max_seconds,
            self.normal_entry_min_seconds,
            self.stop_new_entry_time,
            self.risk_only_time,
            self.max_book_age_seconds,
            self.fee_rate,
        ) < 0:
            raise ValueError("strategy thresholds cannot be negative")


@dataclass
class SimpleTrade:
    trade_id: int
    market_slug: str
    direction: str
    signal_time: str
    signal_price: Decimal
    status: str = "AWAITING_ENTRY_FILL"
    entry_time: str | None = None
    entry_submit_time: str | None = None
    entry_latency_ms: float | None = None
    entry_price: Decimal = ZERO
    entry_qty: Decimal = ZERO
    entry_cost: Decimal = ZERO
    peak_price: Decimal = ZERO
    initial_stop: Decimal = ZERO
    trailing_stop: Decimal = ZERO
    final_stop_price: Decimal = ZERO
    below_stop_ticks: int = 0
    above_profit_ticks: int = 0
    stop_type: str | None = None
    market_speed: str | None = None
    stop_trigger_time: str | None = None
    stop_trigger_price: Decimal | None = None
    stop_penetration: Decimal = ZERO
    hedge_qty: Decimal = ZERO
    hedge_cost: Decimal = ZERO
    hedge_submit_time: str | None = None
    hedge_last_fill_time: str | None = None
    hedge_latency_ms: float | None = None
    hedge_fill_count: int = 0
    exit_qty: Decimal = ZERO
    exit_proceeds: Decimal = ZERO
    exit_submit_time: str | None = None
    exit_last_fill_time: str | None = None
    exit_latency_ms: float | None = None
    exit_fill_count: int = 0
    risk_exit_mode: str | None = None
    result: str | None = None
    final_winner: str | None = None
    original_direction_final_result: str | None = None
    fees: Decimal = ZERO
    gross_pnl: Decimal | None = None
    net_pnl: Decimal | None = None
    pending_submission: bool = False
    pending_role: str | None = None
    submission_uncertain: bool = False
    price_samples: list[tuple[float, Decimal]] = field(default_factory=list)

    @property
    def remaining_exposure(self) -> Decimal:
        return max(ZERO, self.entry_qty - self.hedge_qty - self.exit_qty)

    @property
    def opposite_side(self) -> str:
        return "DOWN" if self.direction == "UP" else "UP"

    @property
    def pair_cost(self) -> Decimal:
        paired = min(self.entry_qty, self.hedge_qty)
        if paired <= 0:
            return ZERO
        entry_unit = self.entry_cost / self.entry_qty
        hedge_unit = self.hedge_cost / self.hedge_qty
        return entry_unit + hedge_unit


@dataclass
class FastDirectionalHedgeSimpleState:
    market_slug: str | None = None
    markets_observed: int = 0
    candidate_side: str | None = None
    candidate_ticks: int = 0
    candidate_signal_price: Decimal | None = None
    candidate_started_at: str | None = None
    last_candidate_book_timestamp: str | None = None
    entries_count: int = 0
    active_trade: SimpleTrade | None = None
    completed_trades: list[SimpleTrade] = field(default_factory=list)


@dataclass(frozen=True)
class SimpleDecision:
    side: str
    quantity: Decimal
    limit_price: Decimal
    executable_price: Decimal
    role: str
    reason: str
    probability: Decimal
    action: str = "BUY"


class FastDirectionalHedgeSimpleEngine:
    def __init__(
        self,
        settings: FastDirectionalHedgeSimpleSettings | None = None,
        state_path: Path | None = None,
        recorder_path: Path | None = None,
    ) -> None:
        self.settings = settings or FastDirectionalHedgeSimpleSettings()
        self.state_path = state_path
        self.recorder_path = recorder_path
        self.state = self._load_state() if state_path else FastDirectionalHedgeSimpleState()

    def begin_market(self, slug: str) -> None:
        if self.state.market_slug == slug:
            return
        completed = [*self.state.completed_trades]
        if self.state.active_trade is not None:
            abandoned = self.state.active_trade
            abandoned.status = (
                "AWAITING_SETTLEMENT"
                if abandoned.status == "DIRECTIONAL"
                else "EXPIRED_WITH_EXPOSURE"
            )
            completed.append(abandoned)
        self.state = FastDirectionalHedgeSimpleState(
            market_slug=slug,
            markets_observed=self.state.markets_observed + 1,
            completed_trades=completed,
        )
        self._save_state()

    def quantities(self) -> tuple[Decimal, Decimal]:
        trade = self.state.active_trade
        if trade is None:
            return ZERO, ZERO
        directional_qty = max(ZERO, trade.entry_qty - trade.exit_qty)
        if trade.direction == "UP":
            return directional_qty, trade.hedge_qty
        return trade.hedge_qty, directional_qty

    def evaluate(
        self,
        *,
        slug: str,
        seconds_to_expiry: Decimal,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
        spot_price: Decimal | None = None,
        price_to_beat: Decimal | None = None,
        sigma_per_sqrt_second: Decimal | None = None,
        observed_at: float | None = None,
    ) -> SimpleDecision | None:
        self.begin_market(slug)
        now = time.time() if observed_at is None else observed_at
        if not self._books_fresh((up_book, down_book), now):
            self._record("skip", {"slug": slug, "reason": "stale_or_unknown_book"})
            return None
        trade = self.state.active_trade
        if trade is not None and trade.submission_uncertain:
            self._record("skip", {"slug": slug, "reason": "submission_requires_reconciliation"})
            return None
        if trade is not None and trade.status in {"DIRECTIONAL", "RISK_EXIT"}:
            return self._manage_trade(
                trade,
                up_book,
                down_book,
                seconds_to_expiry,
                spot_price,
                price_to_beat,
                now,
            )
        if trade is not None and trade.status == "AWAITING_ENTRY_FILL":
            return self._pending_entry_decision(
                trade,
                up_book,
                down_book,
                seconds_to_expiry,
                spot_price,
                price_to_beat,
                sigma_per_sqrt_second,
            )
        if not (
            self.settings.normal_entry_min_seconds
            <= seconds_to_expiry
            <= self.settings.normal_entry_max_seconds
        ):
            return None
        if self.state.entries_count >= self.settings.max_entries_per_window:
            return None
        return self._entry_signal(
            up_book,
            down_book,
            seconds_to_expiry,
            spot_price,
            price_to_beat,
            sigma_per_sqrt_second,
            now,
        )

    def record_fill(
        self,
        slug: str,
        side: str,
        quantity: Decimal,
        cost: Decimal,
        filled_at: str | None = None,
    ) -> None:
        self.begin_market(slug)
        if side not in {"UP", "DOWN"} or quantity <= 0 or cost <= 0:
            raise ValueError("fill must have a valid side, quantity and cost")
        trade = self.state.active_trade
        timestamp = filled_at or datetime.now(timezone.utc).isoformat()
        if trade is None:
            raise RuntimeError("fill received without an active trade")
        if side == trade.direction and trade.status == "AWAITING_ENTRY_FILL":
            trade.entry_qty += quantity
            trade.entry_cost += cost
            trade.entry_price = trade.entry_cost / trade.entry_qty
            trade.entry_time = timestamp
            try:
                trade.entry_latency_ms = (
                    datetime.fromisoformat(timestamp).timestamp()
                    - datetime.fromisoformat(trade.signal_time).timestamp()
                ) * 1000
            except ValueError:
                trade.entry_latency_ms = None
            trade.peak_price = trade.entry_price
            trade.initial_stop = trade.entry_price * (ONE - self.settings.initial_stop_pct)
            trade.final_stop_price = trade.initial_stop
            trade.status = "DIRECTIONAL"
            self.state.entries_count += 1
            event = "entry_fill"
        elif side == trade.opposite_side and trade.status == "RISK_EXIT":
            remaining = trade.remaining_exposure
            applied = min(quantity, remaining)
            if applied <= 0:
                raise RuntimeError("hedge fill would exceed remaining exposure")
            trade.hedge_qty += applied
            trade.hedge_cost += cost * applied / quantity
            trade.hedge_last_fill_time = timestamp
            trade.hedge_fill_count += 1
            if trade.stop_trigger_time is not None:
                try:
                    trade.hedge_latency_ms = (
                        datetime.fromisoformat(timestamp).timestamp()
                        - datetime.fromisoformat(trade.stop_trigger_time).timestamp()
                    ) * 1000
                except ValueError:
                    trade.hedge_latency_ms = None
            if trade.remaining_exposure <= Decimal("0.000001"):
                trade.status = "HEDGED"
                if trade.pair_cost < ONE:
                    trade.result = "HEDGE_LOCKED_PROFIT"
                elif trade.pair_cost == ONE:
                    trade.result = "HEDGE_BREAK_EVEN"
                else:
                    trade.result = "HEDGE_LOCKED_LOSS"
                self.state.completed_trades.append(trade)
                self.state.active_trade = None
                self._reset_candidate()
            event = "hedge_fill"
        else:
            raise RuntimeError("fill side does not match the current strategy state")
        trade.pending_submission = False
        trade.pending_role = None
        self._save_state()
        self._record(event, {"slug": slug, "side": side, "quantity": quantity, "cost": cost, "trade": trade})

    def record_exit_fill(
        self,
        slug: str,
        side: str,
        quantity: Decimal,
        proceeds: Decimal,
        filled_at: str | None = None,
    ) -> None:
        self.begin_market(slug)
        trade = self.state.active_trade
        if trade is None or trade.status != "RISK_EXIT" or trade.risk_exit_mode != "SELL":
            raise RuntimeError("exit fill received without an active sell exit")
        if side != trade.direction or quantity <= 0 or proceeds <= 0:
            raise ValueError("exit fill must match the directional position")
        remaining = trade.remaining_exposure
        applied = min(quantity, remaining)
        if applied <= 0:
            raise RuntimeError("exit fill would exceed remaining exposure")
        timestamp = filled_at or datetime.now(timezone.utc).isoformat()
        trade.exit_qty += applied
        trade.exit_proceeds += proceeds * applied / quantity
        trade.exit_last_fill_time = timestamp
        trade.exit_fill_count += 1
        if trade.stop_trigger_time is not None:
            try:
                trade.exit_latency_ms = (
                    datetime.fromisoformat(timestamp).timestamp()
                    - datetime.fromisoformat(trade.stop_trigger_time).timestamp()
                ) * 1000
            except ValueError:
                trade.exit_latency_ms = None
        trade.pending_submission = False
        trade.pending_role = None
        if trade.remaining_exposure <= Decimal("0.000001"):
            trade.status = "EXITED"
            trade.result = (
                "VOLATILITY_ARBITRAGE_PROFIT"
                if trade.stop_type == "TAKE_PROFIT"
                else "STOP_EXIT"
            )
            self.state.completed_trades.append(trade)
            self.state.active_trade = None
            self._reset_candidate()
        self._save_state()
        self._record(
            "exit_fill",
            {"slug": slug, "side": side, "quantity": applied, "proceeds": proceeds, "trade": trade},
        )

    def mark_submission_started(self, role: str) -> None:
        trade = self.state.active_trade
        if trade is None:
            return
        trade.pending_submission = True
        trade.pending_role = role
        submitted_at = datetime.now(timezone.utc).isoformat()
        if role == "HEDGE":
            trade.hedge_submit_time = submitted_at
        elif role == "EXIT":
            trade.exit_submit_time = submitted_at
        else:
            trade.entry_submit_time = submitted_at
        self._save_state()

    def mark_submission_failed(self, *, uncertain: bool) -> None:
        trade = self.state.active_trade
        if trade is None:
            return
        role = trade.pending_role
        trade.pending_submission = False
        trade.submission_uncertain = uncertain
        if not uncertain and role == "ENTRY" and trade.status == "AWAITING_ENTRY_FILL":
            self.state.active_trade = None
            self._reset_candidate()
        self._save_state()

    def reconcile_positions(self, up_qty: Decimal, down_qty: Decimal) -> None:
        """Reconcile persisted exposure with current-market token balances.

        This is intentionally conservative: unexplained wallet inventory or an
        opposite balance larger than the directional balance blocks startup
        instead of guessing and potentially submitting a duplicate hedge.
        """
        if up_qty < 0 or down_qty < 0:
            raise ValueError("reconciled token balances cannot be negative")
        trade = self.state.active_trade
        if trade is None:
            if up_qty > Decimal("0.000001") or down_qty > Decimal("0.000001"):
                raise RuntimeError("wallet has current-market inventory absent from local strategy state")
            return
        directional_qty = up_qty if trade.direction == "UP" else down_qty
        opposite_qty = down_qty if trade.direction == "UP" else up_qty
        if opposite_qty > directional_qty + Decimal("0.000001"):
            raise RuntimeError("opposite token balance exceeds directional inventory")
        if trade.status == "AWAITING_ENTRY_FILL":
            if directional_qty <= Decimal("0.000001"):
                trade.pending_submission = False
                trade.submission_uncertain = False
                self.state.active_trade = None
                self._reset_candidate()
                return
            trade.entry_qty = directional_qty
            trade.entry_price = trade.signal_price
            trade.entry_cost = directional_qty * trade.signal_price
            trade.entry_time = datetime.now(timezone.utc).isoformat()
            trade.peak_price = trade.entry_price
            trade.initial_stop = trade.entry_price * (ONE - self.settings.initial_stop_pct)
            trade.final_stop_price = trade.initial_stop
            trade.status = "DIRECTIONAL"
            self.state.entries_count += 1
        elif trade.risk_exit_mode == "SELL" and directional_qty <= trade.entry_qty:
            trade.exit_qty = trade.entry_qty - directional_qty
        elif abs(directional_qty - (trade.entry_qty - trade.exit_qty)) > Decimal("0.000001"):
            raise RuntimeError("wallet directional balance disagrees with persisted entry quantity")
        trade.hedge_qty = opposite_qty
        if trade.hedge_qty >= trade.entry_qty - Decimal("0.000001"):
            trade.status = "HEDGED"
            self.state.completed_trades.append(trade)
            self.state.active_trade = None
        elif opposite_qty > 0 or trade.status == "RISK_EXIT":
            trade.status = "RISK_EXIT"
        trade.pending_submission = False
        trade.pending_role = None
        trade.submission_uncertain = False
        self._save_state()

    def record_settlement(self, slug: str, winner: str) -> None:
        if winner not in {"UP", "DOWN"}:
            raise ValueError("winner must be UP or DOWN")
        trades = [trade for trade in self.state.completed_trades if trade.market_slug == slug]
        if self.state.active_trade is not None and self.state.active_trade.market_slug == slug:
            trades.append(self.state.active_trade)
        changed = False
        for trade in trades:
            if trade.final_winner is not None:
                continue
            trade.final_winner = winner
            trade.original_direction_final_result = "WIN" if trade.direction == winner else "LOSS"
            directional_qty = max(ZERO, trade.entry_qty - trade.exit_qty)
            payout_qty = directional_qty if trade.direction == winner else trade.hedge_qty
            trade.fees = (
                trade.entry_qty
                * self.settings.fee_rate
                * trade.entry_price
                * (ONE - trade.entry_price)
            )
            if trade.hedge_qty > 0:
                hedge_price = trade.hedge_cost / trade.hedge_qty
                trade.fees += (
                    trade.hedge_qty
                    * self.settings.fee_rate
                    * hedge_price
                    * (ONE - hedge_price)
                )
            if trade.exit_qty > 0:
                exit_price = trade.exit_proceeds / trade.exit_qty
                trade.fees += (
                    trade.exit_qty
                    * self.settings.fee_rate
                    * exit_price
                    * (ONE - exit_price)
                )
            trade.gross_pnl = payout_qty + trade.exit_proceeds - trade.entry_cost - trade.hedge_cost
            trade.net_pnl = trade.gross_pnl - trade.fees
            if trade.result is None:
                trade.result = "WIN" if trade.direction == winner else "LOSS"
            changed = True
        if changed:
            self._save_state()
            self._record("settlement", {"slug": slug, "winner": winner, "trades": trades})

    def _entry_signal(
        self,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
        seconds_to_expiry: Decimal,
        spot_price: Decimal | None,
        price_to_beat: Decimal | None,
        sigma_per_sqrt_second: Decimal | None,
        now: float,
    ) -> SimpleDecision | None:
        up_ask, down_ask = _best_ask(up_book), _best_ask(down_book)
        if up_ask is None or down_ask is None or up_ask == down_ask:
            self._reset_candidate()
            return None
        side = "UP" if up_ask > down_ask else "DOWN"
        book = up_book if side == "UP" else down_book
        ask = up_ask if side == "UP" else down_ask
        if not self.settings.entry_price_min <= ask <= self.settings.entry_price_max:
            self._reset_candidate()
            return None
        if abs(up_ask - down_ask) < self.settings.min_ask_gap:
            self._reset_candidate()
            return None
        if up_ask + down_ask > self.settings.max_ask_sum:
            self._reset_candidate()
            return None
        bid = max(
            (level.price for level in book.bids if level.price > 0 and level.size > 0),
            default=None,
        )
        if bid is None or ask - bid > self.settings.max_spread:
            self._reset_candidate()
            return None
        if side != self.state.candidate_side:
            self.state.candidate_side = side
            self.state.candidate_ticks = 1
            self.state.candidate_signal_price = ask
            self.state.candidate_started_at = _utc_iso(now)
            self.state.last_candidate_book_timestamp = book.timestamp
            self._save_state()
            return None
        candidate_started = (
            datetime.fromisoformat(self.state.candidate_started_at).timestamp()
            if self.state.candidate_started_at is not None
            else now
        )
        if (
            book.timestamp != self.state.last_candidate_book_timestamp
            and (now - candidate_started) * 1000 >= self.settings.entry_confirm_min_interval_ms
        ):
            self.state.candidate_ticks += 1
            self.state.last_candidate_book_timestamp = book.timestamp
        if self.state.candidate_ticks < self.settings.entry_confirm_ticks:
            self._save_state()
            return None
        signal_price = self.state.candidate_signal_price or ask
        if ask - signal_price > self.settings.max_entry_drift:
            self._reset_candidate()
            return None
        execution = _vwap(book, self.settings.base_position_size, buy=True)
        if execution is None:
            return None
        executable, worst, available = execution
        if available < self.settings.base_position_size:
            quantity = available
        else:
            quantity = self.settings.base_position_size
        limit = min(
            self.settings.entry_price_max,
            signal_price + self.settings.max_entry_drift,
            worst + self.settings.entry_max_slippage,
        )
        if worst > limit or quantity <= 0:
            return None
        trade = SimpleTrade(
            trade_id=self.state.entries_count + 1,
            market_slug=self.state.market_slug or "",
            direction=side,
            signal_time=self.state.candidate_started_at or _utc_iso(now),
            signal_price=signal_price,
        )
        self.state.active_trade = trade
        self._save_state()
        return SimpleDecision(
            side=side,
            quantity=quantity.quantize(Decimal("0.000001"), rounding=ROUND_DOWN),
            limit_price=limit,
            executable_price=executable,
            role="ENTRY",
            probability=ask / (up_ask + down_ask),
            reason=(
                f"book_volatility_arbitrage_v2.0 role=ENTRY ticks={self.state.candidate_ticks} "
                f"signal_price={signal_price:.4f} executable={executable:.4f} "
                f"ask_gap={abs(up_ask - down_ask):.4f} seconds_left={seconds_to_expiry:.1f}"
            ),
        )

    def execution_summary(self) -> dict[str, Any]:
        trades = [*self.state.completed_trades]
        if self.state.active_trade is not None:
            trades.append(self.state.active_trade)
        entry_latencies = [trade.entry_latency_ms for trade in trades if trade.entry_latency_ms is not None]
        hedge_latencies = [trade.hedge_latency_ms for trade in trades if trade.hedge_latency_ms is not None]
        exit_latencies = [trade.exit_latency_ms for trade in trades if trade.exit_latency_ms is not None]

        def percentile95(values: list[float]) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))]

        return {
            "markets_observed": self.state.markets_observed,
            "trades": len(trades),
            "average_entry_latency_ms": sum(entry_latencies) / len(entry_latencies) if entry_latencies else None,
            "p95_entry_latency_ms": percentile95(entry_latencies),
            "average_hedge_latency_ms": sum(hedge_latencies) / len(hedge_latencies) if hedge_latencies else None,
            "p95_hedge_latency_ms": percentile95(hedge_latencies),
            "average_exit_latency_ms": sum(exit_latencies) / len(exit_latencies) if exit_latencies else None,
            "p95_exit_latency_ms": percentile95(exit_latencies),
            "partial_hedge_count": sum(trade.hedge_fill_count > 1 for trade in trades),
            "partial_exit_count": sum(trade.exit_fill_count > 1 for trade in trades),
            "normal_stop_count": sum(trade.market_speed == "NORMAL" for trade in trades),
            "fast_stop_count": sum(trade.market_speed == "FAST" for trade in trades),
            "emergency_stop_count": sum(trade.market_speed == "EMERGENCY" for trade in trades),
            "hedge_locked_profit_count": sum(trade.result == "HEDGE_LOCKED_PROFIT" for trade in trades),
            "hedge_break_even_count": sum(trade.result == "HEDGE_BREAK_EVEN" for trade in trades),
            "hedge_locked_loss_count": sum(trade.result == "HEDGE_LOCKED_LOSS" for trade in trades),
            "stop_exit_count": sum(trade.result == "STOP_EXIT" for trade in trades),
            "volatility_take_profit_count": sum(
                trade.result == "VOLATILITY_ARBITRAGE_PROFIT" for trade in trades
            ),
            "settlement_win_count": sum(trade.result == "WIN" for trade in trades),
            "stop_recovery_rate": (
                sum(
                    trade.stop_trigger_time is not None
                    and trade.original_direction_final_result == "WIN"
                    for trade in trades
                )
                / sum(trade.stop_trigger_time is not None for trade in trades)
                if any(trade.stop_trigger_time is not None for trade in trades)
                else None
            ),
            "gross_pnl": sum((trade.gross_pnl or ZERO) for trade in trades),
            "fees": sum((trade.fees or ZERO) for trade in trades),
            "net_pnl": sum((trade.net_pnl or ZERO) for trade in trades),
        }

    def _pending_entry_decision(
        self,
        trade: SimpleTrade,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
        seconds_to_expiry: Decimal,
        spot_price: Decimal | None,
        price_to_beat: Decimal | None,
        sigma_per_sqrt_second: Decimal | None,
    ) -> SimpleDecision | None:
        book = up_book if trade.direction == "UP" else down_book
        ask = _best_ask(book)
        if ask is None or not self.settings.entry_price_min <= ask <= self.settings.entry_price_max:
            return None
        if not (
            self.settings.normal_entry_min_seconds
            <= seconds_to_expiry
            <= self.settings.normal_entry_max_seconds
        ):
            return None
        up_ask, down_ask = _best_ask(up_book), _best_ask(down_book)
        if up_ask is None or down_ask is None:
            return None
        if abs(up_ask - down_ask) < self.settings.min_ask_gap or up_ask + down_ask > self.settings.max_ask_sum:
            return None
        bid = max(
            (level.price for level in book.bids if level.price > 0 and level.size > 0),
            default=None,
        )
        if bid is None or ask - bid > self.settings.max_spread:
            return None
        if ask - trade.signal_price > self.settings.max_entry_drift:
            return None
        execution = _vwap(book, self.settings.base_position_size, buy=True)
        if execution is None:
            return None
        executable, worst, available = execution
        quantity = min(self.settings.base_position_size, available)
        limit = min(
            self.settings.entry_price_max,
            trade.signal_price + self.settings.max_entry_drift,
            worst + self.settings.entry_max_slippage,
        )
        if quantity <= 0 or worst > limit:
            return None
        opposite_ask = _best_ask(down_book if trade.direction == "UP" else up_book)
        total = ask + opposite_ask if opposite_ask is not None else ONE
        return SimpleDecision(
            side=trade.direction,
            quantity=quantity.quantize(Decimal("0.000001"), rounding=ROUND_DOWN),
            limit_price=limit,
            executable_price=executable,
            role="ENTRY",
            probability=ask / total if total > 0 else ask,
            reason=(
                f"book_volatility_arbitrage_v2.0 role=ENTRY ticks={self.state.candidate_ticks} "
                f"signal_price={trade.signal_price:.4f} executable={executable:.4f}"
            ),
        )

    def _manage_trade(
        self,
        trade: SimpleTrade,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
        seconds_to_expiry: Decimal,
        spot_price: Decimal | None,
        price_to_beat: Decimal | None,
        now: float,
    ) -> SimpleDecision | None:
        if trade.status == "RISK_EXIT":
            return self._risk_exit_decision(trade, up_book, down_book, seconds_to_expiry)
        held_book = up_book if trade.direction == "UP" else down_book
        executable = _vwap(held_book, trade.remaining_exposure, buy=False)
        # Missing/partial exit depth is adverse information, not a reason to
        # suspend risk management. Use the executable portion, or zero when no
        # bid exists, so a liquidity vacuum escalates to an emergency hedge.
        current_price = executable[0] if executable is not None else ZERO
        trade.peak_price = max(trade.peak_price, current_price)
        executable_net_profit = self._net_exit_profit_per_share(trade, current_price)
        if executable_net_profit >= self.settings.take_profit_net_per_share:
            trade.above_profit_ticks += 1
        else:
            trade.above_profit_ticks = 0
        if trade.above_profit_ticks >= self.settings.take_profit_confirm_ticks:
            trade.status = "RISK_EXIT"
            trade.risk_exit_mode = "SELL"
            trade.stop_type = "TAKE_PROFIT"
            trade.market_speed = "NORMAL"
            trade.stop_trigger_time = _utc_iso(now)
            trade.stop_trigger_price = current_price
            trade.stop_penetration = ZERO
            self._save_state()
            self._record(
                "take_profit_trigger",
                {
                    "trade": trade,
                    "executable_net_profit_per_share": executable_net_profit,
                },
            )
            return self._sell_decision(trade, up_book, down_book)
        if self.settings.trailing_enabled and trade.peak_price >= trade.entry_price + self.settings.trailing_start_gain:
            trade.trailing_stop = trade.peak_price * (ONE - self.settings.trailing_drawdown_pct)
            break_even = (
                trade.entry_price + self.settings.break_even_buffer
                if self.settings.break_even_enabled
                else ZERO
            )
            trade.final_stop_price = max(trade.final_stop_price, trade.initial_stop, trade.trailing_stop, break_even)
        else:
            trade.final_stop_price = max(trade.final_stop_price, trade.initial_stop)
        trade.price_samples.append((now, current_price))
        cutoff = now - max(1.0, self.settings.fast_move_window_ms / 1000 * 3)
        trade.price_samples = [sample for sample in trade.price_samples if sample[0] >= cutoff]
        if current_price >= trade.final_stop_price:
            trade.below_stop_ticks = 0
            self._save_state()
            return None
        trade.below_stop_ticks += 1
        penetration = trade.final_stop_price - current_price
        fast_move = self._fast_move(trade, now)
        if penetration >= self.settings.emergency_stop_penetration:
            speed, required = "EMERGENCY", 1
        elif fast_move >= self.settings.fast_move_threshold:
            speed, required = "FAST", self.settings.fast_stop_confirm_ticks
        else:
            speed, required = "NORMAL", self.settings.stop_confirm_ticks
        if trade.below_stop_ticks < required:
            self._save_state()
            return None
        trade.status = "RISK_EXIT"
        trade.stop_type = "TRAILING" if trade.trailing_stop > 0 else "INITIAL"
        trade.market_speed = speed
        trade.stop_trigger_time = _utc_iso(now)
        trade.stop_trigger_price = current_price
        trade.stop_penetration = penetration
        self._save_state()
        self._record("stop_trigger", {"trade": trade})
        return self._risk_exit_decision(trade, up_book, down_book, seconds_to_expiry)

    def _risk_exit_decision(
        self,
        trade: SimpleTrade,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
        seconds_to_expiry: Decimal,
    ) -> SimpleDecision | None:
        if trade.risk_exit_mode == "SELL":
            return self._sell_decision(trade, up_book, down_book)
        if trade.risk_exit_mode == "HEDGE":
            return self._hedge_decision(trade, up_book, down_book)

        remaining = trade.remaining_exposure
        held_book = up_book if trade.direction == "UP" else down_book
        opposite_book = down_book if trade.direction == "UP" else up_book
        sell_execution = _vwap(held_book, remaining, buy=False)
        hedge_execution = None
        if (
            self.settings.prefer_hedge
            and self.settings.hedge_entry_min_seconds
            <= seconds_to_expiry
            <= self.settings.hedge_entry_max_seconds
        ):
            candidate = _vwap(opposite_book, remaining, buy=True)
            if candidate is not None and candidate[1] <= self.settings.hedge_max_price:
                hedge_execution = candidate

        sell_net_value = None
        if sell_execution is not None and sell_execution[2] >= remaining:
            sell_price = sell_execution[0]
            sell_net_value = sell_price - (
                self.settings.fee_rate * sell_price * (ONE - sell_price)
            )
        hedge_net_value = None
        if hedge_execution is not None and hedge_execution[2] >= remaining:
            hedge_price = hedge_execution[0]
            hedge_net_value = ONE - hedge_price - (
                self.settings.fee_rate * hedge_price * (ONE - hedge_price)
            )

        if sell_net_value is not None and (
            hedge_net_value is None or sell_net_value >= hedge_net_value
        ):
            trade.risk_exit_mode = "SELL"
        elif hedge_net_value is not None:
            trade.risk_exit_mode = "HEDGE"
        else:
            self._save_state()
            return None
        self._save_state()
        self._record(
            "risk_exit_route",
            {
                "trade": trade,
                "sell_net_value_per_share": sell_net_value,
                "hedge_net_value_per_share": hedge_net_value,
                "seconds_to_expiry": seconds_to_expiry,
            },
        )
        if trade.risk_exit_mode == "SELL":
            return self._sell_decision(trade, up_book, down_book)
        return self._hedge_decision(trade, up_book, down_book)

    def _sell_decision(
        self,
        trade: SimpleTrade,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
    ) -> SimpleDecision | None:
        remaining = trade.remaining_exposure
        if remaining <= Decimal("0.000001"):
            return None
        book = up_book if trade.direction == "UP" else down_book
        execution = _vwap(book, remaining, buy=False)
        if execution is None:
            return None
        executable, worst, available = execution
        quantity = min(remaining, available)
        if quantity <= 0:
            return None
        if trade.stop_type == "TAKE_PROFIT":
            net_profit = self._net_exit_profit_per_share(trade, executable)
            if net_profit < self.settings.take_profit_net_per_share:
                trade.status = "DIRECTIONAL"
                trade.risk_exit_mode = None
                trade.stop_type = None
                trade.market_speed = None
                trade.stop_trigger_time = None
                trade.stop_trigger_price = None
                trade.above_profit_ticks = 0
                self._save_state()
                self._record(
                    "take_profit_cancelled",
                    {
                        "trade": trade,
                        "executable_net_profit_per_share": net_profit,
                    },
                )
                return None
            # A profit-taking order may miss, but it must never chase through
            # the observed profitable bid and turn a spread capture into loss.
            limit = worst
        else:
            limit = max(Decimal("0.01"), worst - self.settings.exit_max_slippage)
        return SimpleDecision(
            side=trade.direction,
            quantity=quantity.quantize(Decimal("0.000001"), rounding=ROUND_DOWN),
            limit_price=limit,
            executable_price=executable,
            role="EXIT",
            probability=executable,
            action="SELL",
            reason=(
                f"book_volatility_arbitrage_v2.0 role=EXIT exit_type={trade.stop_type} "
                f"speed={trade.market_speed} stop={trade.final_stop_price:.4f} "
                f"trigger={trade.stop_trigger_price or ZERO:.4f} remaining={remaining:.6f}"
            ),
        )

    def _hedge_decision(
        self,
        trade: SimpleTrade,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
    ) -> SimpleDecision | None:
        remaining = trade.remaining_exposure
        if remaining <= Decimal("0.000001"):
            return None
        book = down_book if trade.direction == "UP" else up_book
        best_ask = _best_ask(book)
        if best_ask is None:
            return None
        limit = min(
            self.settings.hedge_max_price,
            best_ask + self.settings.hedge_max_slippage,
        )
        available = sum(
            (level.size for level in book.asks if level.price <= limit and level.size > 0),
            ZERO,
        )
        quantity = min(remaining, available)
        if quantity <= 0:
            return None
        execution = _vwap(book, quantity, buy=True)
        if execution is None:
            return None
        return SimpleDecision(
            side=trade.opposite_side,
            quantity=quantity.quantize(Decimal("0.000001"), rounding=ROUND_DOWN),
            limit_price=limit,
            executable_price=execution[0],
            role="HEDGE",
            probability=best_ask,
            reason=(
                f"book_volatility_arbitrage_v2.0 role=HEDGE stop_type={trade.stop_type} "
                f"speed={trade.market_speed} stop={trade.final_stop_price:.4f} "
                f"trigger={trade.stop_trigger_price or ZERO:.4f} penetration={trade.stop_penetration:.4f} "
                f"remaining={remaining:.6f}"
            ),
        )

    def _fast_move(self, trade: SimpleTrade, now: float) -> Decimal:
        target = now - self.settings.fast_move_window_ms / 1000
        candidates = [sample for sample in trade.price_samples[:-1] if sample[0] <= target]
        if not trade.price_samples:
            return ZERO
        reference = candidates[-1] if candidates else trade.price_samples[0]
        return abs(trade.price_samples[-1][1] - reference[1])

    def _net_exit_profit_per_share(
        self,
        trade: SimpleTrade,
        exit_price: Decimal,
    ) -> Decimal:
        entry_fee = self.settings.fee_rate * trade.entry_price * (ONE - trade.entry_price)
        exit_fee = self.settings.fee_rate * exit_price * (ONE - exit_price)
        return exit_price - trade.entry_price - entry_fee - exit_fee

    def _books_fresh(self, books: tuple[OrderBookSnapshot, OrderBookSnapshot], now: float) -> bool:
        for book in books:
            try:
                timestamp = float(book.timestamp)
                if timestamp > 10_000_000_000:
                    timestamp /= 1000
            except (TypeError, ValueError):
                return False
            if Decimal(str(max(0.0, now - timestamp))) > self.settings.max_book_age_seconds:
                return False
        return True

    def _reset_candidate(self) -> None:
        self.state.candidate_side = None
        self.state.candidate_ticks = 0
        self.state.candidate_signal_price = None
        self.state.candidate_started_at = None
        self.state.last_candidate_book_timestamp = None
        self._save_state()

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(asdict(self.state), default=str, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _load_state(self) -> FastDirectionalHedgeSimpleState:
        if self.state_path is None or not self.state_path.exists():
            return FastDirectionalHedgeSimpleState()
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        active = payload.get("active_trade")
        completed = payload.get("completed_trades") or []
        for raw in [item for item in [active, *completed] if item]:
            for key in (
                "signal_price", "entry_price", "entry_qty", "entry_cost", "peak_price",
                "initial_stop", "trailing_stop", "final_stop_price", "stop_penetration",
                "hedge_qty", "hedge_cost",
                "exit_qty", "exit_proceeds",
                "fees", "gross_pnl", "net_pnl",
            ):
                raw[key] = (
                    Decimal(str(raw[key]))
                    if raw.get(key) is not None
                    else None
                    if key in {"gross_pnl", "net_pnl"}
                    else ZERO
                )
            raw["stop_trigger_price"] = (
                Decimal(str(raw["stop_trigger_price"])) if raw.get("stop_trigger_price") is not None else None
            )
            raw["price_samples"] = [(float(ts), Decimal(str(price))) for ts, price in raw.get("price_samples", [])]
        return FastDirectionalHedgeSimpleState(
            market_slug=payload.get("market_slug"),
            markets_observed=int(payload.get("markets_observed", int(payload.get("market_slug") is not None))),
            candidate_side=payload.get("candidate_side"),
            candidate_ticks=int(payload.get("candidate_ticks", 0)),
            candidate_signal_price=(Decimal(str(payload["candidate_signal_price"])) if payload.get("candidate_signal_price") is not None else None),
            candidate_started_at=payload.get("candidate_started_at"),
            last_candidate_book_timestamp=payload.get("last_candidate_book_timestamp"),
            entries_count=int(payload.get("entries_count", 0)),
            active_trade=SimpleTrade(**active) if active else None,
            completed_trades=[SimpleTrade(**item) for item in completed],
        )

    def _record(self, event: str, payload: dict[str, Any]) -> None:
        if self.recorder_path is None:
            return
        self.recorder_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"recorded_at": datetime.now(timezone.utc).isoformat(), "strategy_id": self.settings.strategy_id, "version": self.settings.version, "event": event, **payload}
        with self.recorder_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, default=lambda value: asdict(value) if hasattr(value, "__dataclass_fields__") else str(value), ensure_ascii=False) + "\n")
