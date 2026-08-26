from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Callable, Protocol

from src.crypto_resolution import resolve_btc_5m_twap
from src.polymarket import Market, OrderBookSnapshot
from src.polygon_split import CompleteSetSplitter
from src.reversal_v11 import (
    ActiveRound,
    Direction,
    MarketHealth,
    ReversalSettings,
    ReversalV11,
    TradePlan,
    format_daily_report,
)


class ReversalRuntimeError(RuntimeError):
    pass


class GammaResultMismatch(ReversalRuntimeError):
    def __init__(
        self,
        message: str,
        *,
        slug: str,
        provisional: Direction,
        official: Direction,
    ) -> None:
        super().__init__(message)
        self.slug = slug
        self.provisional = provisional
        self.official = official


class ChainResultMismatch(GammaResultMismatch):
    pass


logger = logging.getLogger(__name__)
MIN_MARKETABLE_BUY_NOTIONAL = Decimal("1.01")
ORDER_SIZE_QUANTUM = Decimal("0.01")
MAKER_AMOUNT_QUANTUM = Decimal("0.01")
GAMMA_CORRECTION_MIN_SECONDS_LEFT = Decimal("30")
GAMMA_CORRECTION_MAX_ASK = Decimal("0.80")


class Splitter(Protocol):
    def split(
        self,
        *,
        condition_id: str,
        up_token_id: str,
        down_token_id: str,
        amount: Decimal,
        neg_risk: bool,
    ) -> Any: ...

    def merge(
        self,
        *,
        condition_id: str,
        up_token_id: str,
        down_token_id: str,
        amount: Decimal,
        neg_risk: bool,
    ) -> Any: ...


class ExitTrader(Protocol):
    def buy_limit(
        self,
        token_id: str,
        price: Decimal,
        size: Decimal,
        tick_size: str,
        neg_risk: bool,
        order_type: str = "GTC",
        submit_not_after_monotonic: float | None = None,
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
    ) -> dict[str, Any]: ...

    def conditional_balance(self, token_id: str, signature_type: int) -> Decimal: ...

    def collateral_balance(self, signature_type: int) -> Decimal: ...


@dataclass(frozen=True)
class ReversalTickResult:
    status: str
    plan: TradePlan | None = None
    order: dict[str, Any] | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ReversalStartupReport:
    wallet: str
    collateral_units: int
    open_orders: int
    up_balance: Decimal
    down_balance: Decimal
    relayer_deployed: bool


@dataclass(frozen=True)
class HistoricalAuditTask:
    source: str
    slug: str
    expected: Direction
    deadline: float


@dataclass(frozen=True)
class HistoricalAuditResult:
    source: str
    slug: str
    expected: Direction
    published: str | None = None
    error: str | None = None


class HistoricalAuditWorker:
    """Run slow Polygon/Gamma history lookups away from the trading loop."""

    def __init__(
        self,
        *,
        gamma_lookup: Callable[[str], str | None],
        chain_lookup: Callable[[str], str | None] | None,
    ) -> None:
        self.gamma_lookup = gamma_lookup
        self.chain_lookup = chain_lookup
        self.tasks: queue.Queue[HistoricalAuditTask] = queue.Queue()
        self.results: queue.Queue[HistoricalAuditResult] = queue.Queue()
        self.thread = threading.Thread(
            target=self._run,
            name="reversal-history-audit",
            daemon=True,
        )
        self.thread.start()

    def submit(self, task: HistoricalAuditTask) -> None:
        self.tasks.put_nowait(task)

    def _run(self) -> None:
        while True:
            task = self.tasks.get()
            if time.monotonic() > task.deadline:
                self.results.put(
                    HistoricalAuditResult(
                        source=task.source,
                        slug=task.slug,
                        expected=task.expected,
                        error="audit batch deadline expired before lookup",
                    )
                )
                continue
            lookup = self.chain_lookup if task.source == "chain" else self.gamma_lookup
            if lookup is None:
                self.results.put(
                    HistoricalAuditResult(
                        source=task.source,
                        slug=task.slug,
                        expected=task.expected,
                        error="audit source unavailable",
                    )
                )
                continue
            try:
                published = lookup(task.slug)
            except Exception as exc:
                self.results.put(
                    HistoricalAuditResult(
                        source=task.source,
                        slug=task.slug,
                        expected=task.expected,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            else:
                self.results.put(
                    HistoricalAuditResult(
                        source=task.source,
                        slug=task.slug,
                        expected=task.expected,
                        published=published,
                    )
                )


@dataclass(frozen=True)
class DynamicRecoveryDecision:
    allowed: bool
    shares: Decimal = Decimal("0")
    expected_value_per_share: Decimal = Decimal("0")
    projected_streak_loss: Decimal = Decimal("0")
    reason: str = ""


def dynamic_recovery_decision(
    *,
    cumulative_loss: Decimal,
    entry_price: Decimal,
    retained_side: Direction,
    retained_probability: Decimal | None,
    spot_price: Decimal | None,
    open_price: Decimal | None,
    settings: ReversalSettings,
) -> DynamicRecoveryDecision:
    """Size the final attempt from recovery value and worst-case streak risk."""
    if spot_price is None or open_price is None:
        return DynamicRecoveryDecision(False, reason="BTC open/curr price unavailable")
    cross_distance = (
        spot_price - open_price
        if retained_side is Direction.UP
        else open_price - spot_price
    )
    if cross_distance < settings.recovery_min_open_cross_usd:
        return DynamicRecoveryDecision(
            False,
            reason=(
                f"BTC open cross {cross_distance:.4f} below "
                f"{settings.recovery_min_open_cross_usd} USD buffer"
            ),
        )
    if not settings.recovery_min_entry_price <= entry_price <= settings.recovery_max_entry_price:
        return DynamicRecoveryDecision(
            False,
            reason=(
                f"entry ask {entry_price} outside "
                f"{settings.recovery_min_entry_price}-{settings.recovery_max_entry_price}"
            ),
        )
    if cumulative_loss <= 0:
        return DynamicRecoveryDecision(False, reason="no measured prior loss to recover")
    if retained_probability is None or not Decimal("0") <= retained_probability <= Decimal("1"):
        return DynamicRecoveryDecision(False, reason="live retained-side probability unavailable")

    fee_per_share = Decimal("0.07") * entry_price * (Decimal("1") - entry_price)
    expected_value = retained_probability - entry_price - fee_per_share
    if expected_value < settings.recovery_min_expected_value:
        return DynamicRecoveryDecision(
            False,
            expected_value_per_share=expected_value,
            reason=f"expected value {expected_value:.4f} below threshold",
        )
    win_gain = Decimal("1") - entry_price - fee_per_share
    loss_per_share = entry_price + fee_per_share
    remaining_risk = settings.maximum_streak_loss - cumulative_loss
    if win_gain <= 0 or remaining_risk <= 0:
        return DynamicRecoveryDecision(False, reason="streak loss limit already exhausted")

    required_shares = cumulative_loss * settings.recovery_fraction / win_gain
    risk_limited_shares = remaining_risk / loss_per_share
    if required_shares > risk_limited_shares:
        return DynamicRecoveryDecision(
            False,
            expected_value_per_share=expected_value,
            reason="recovery target would exceed the streak loss limit",
        )
    if required_shares > settings.recovery_max_shares:
        return DynamicRecoveryDecision(
            False,
            expected_value_per_share=expected_value,
            reason="required position exceeds the 16-share hard cap",
        )

    shares = _marketable_buy_size(
        nominal_shares=required_shares,
        price=entry_price,
    )
    projected_loss = cumulative_loss + shares * loss_per_share
    if shares > settings.recovery_max_shares or projected_loss > settings.maximum_streak_loss:
        return DynamicRecoveryDecision(
            False,
            expected_value_per_share=expected_value,
            projected_streak_loss=projected_loss,
            reason="exchange-valid size would breach a risk limit",
        )
    return DynamicRecoveryDecision(
        True,
        shares=shares,
        expected_value_per_share=expected_value,
        projected_streak_loss=projected_loss,
        reason=f"recover {settings.recovery_fraction:.0%} of prior loss",
    )


def full_loss_recovery_size(
    *,
    cumulative_loss: Decimal,
    entry_price: Decimal,
    recovery_fraction: Decimal = Decimal("1"),
    minimum_profit: Decimal = Decimal("0"),
    minimum_shares: Decimal = Decimal("0"),
    filled_shares: Decimal = Decimal("0"),
    filled_cost: Decimal = Decimal("0"),
    filled_fees: Decimal = Decimal("0"),
) -> Decimal:
    """Size a stage to cover prior loss and a minimum net round profit."""
    if cumulative_loss < 0:
        raise ValueError("full-loss recovery requires non-negative cumulative loss")
    if not Decimal("0") < entry_price < Decimal("1"):
        raise ValueError("full-loss recovery entry price must be between zero and one")
    if not Decimal("0") < recovery_fraction <= Decimal("1"):
        raise ValueError("recovery fraction must be in (0, 1]")
    if minimum_profit < 0:
        raise ValueError("minimum profit must not be negative")
    if min(minimum_shares, filled_shares, filled_cost, filled_fees) < 0:
        raise ValueError("filled recovery amounts must not be negative")
    recovered_if_win = filled_shares - filled_cost - filled_fees
    recovery_target = cumulative_loss * recovery_fraction + minimum_profit
    remaining_loss = max(Decimal("0"), recovery_target - recovered_if_win)
    if remaining_loss == 0:
        return max(minimum_shares, filled_shares)
    fee_per_share = Decimal("0.07") * entry_price * (Decimal("1") - entry_price)
    net_win_per_share = Decimal("1") - entry_price - fee_per_share
    if net_win_per_share <= 0:
        raise ValueError("full-loss recovery has no positive net payout at this price")
    additional = _marketable_buy_size(
        nominal_shares=remaining_loss / net_win_per_share,
        price=entry_price,
    )
    return max(minimum_shares, filled_shares + additional)


def reversal_startup_self_check(
    *,
    market: Market,
    splitter: CompleteSetSplitter | None,
    trader: ExitTrader,
    signature_type: int,
    required_collateral: Decimal = Decimal("30"),
    execution_mode: str = "split_sell",
    wallet: str = "CLOB funder",
    require_trade_collateral: bool = True,
) -> ReversalStartupReport:
    if signature_type != 3:
        raise ReversalRuntimeError("reversal_v11 live execution requires SIGNATURE_TYPE=3")
    open_orders = trader.open_orders()  # type: ignore[attr-defined]
    if open_orders:
        raise ReversalRuntimeError(
            f"startup blocked: {len(open_orders)} existing CLOB order(s) require review"
        )
    up_balance = trader.conditional_balance(market.token_ids[0], signature_type)
    down_balance = trader.conditional_balance(market.token_ids[1], signature_type)
    if execution_mode == "direct_buy":
        collateral = trader.collateral_balance(signature_type)
        required_collateral = (
            max(required_collateral, MIN_MARKETABLE_BUY_NOTIONAL)
            if require_trade_collateral
            else Decimal("0")
        )
        if require_trade_collateral and collateral < required_collateral:
            raise ReversalRuntimeError(
                f"startup blocked: collateral {collateral} is below required "
                f"{required_collateral} pUSD"
            )
        return ReversalStartupReport(
            wallet=wallet,
            collateral_units=int(collateral * Decimal("1000000")),
            open_orders=0,
            up_balance=up_balance,
            down_balance=down_balance,
            relayer_deployed=False,
        )
    if splitter is None:
        raise ReversalRuntimeError("split_sell startup requires a configured splitter")
    check_amount = max(Decimal("0.000001"), required_collateral)
    preflight = splitter.preflight(
        condition_id=market.condition_id,
        up_token_id=market.token_ids[0],
        down_token_id=market.token_ids[1],
        amount=check_amount,
        neg_risk=market.neg_risk,
    )
    relayer_check = getattr(splitter.submitter, "read_only_self_check", None)
    if relayer_check is None:
        raise ReversalRuntimeError("configured split submitter has no read-only Relayer self-check")
    relayer = relayer_check()
    if not relayer.get("deployed"):
        raise ReversalRuntimeError("Relayer reports that the Deposit Wallet is not deployed")
    relayer_prewarm = getattr(splitter.submitter, "prewarm", None)
    if relayer_prewarm is not None:
        relayer_prewarm()
    return ReversalStartupReport(
        wallet=preflight.wallet,
        collateral_units=preflight.collateral_balance_units,
        open_orders=0,
        up_balance=up_balance,
        down_balance=down_balance,
        relayer_deployed=True,
    )


def previous_5m_slug(slug: str, count: int = 1) -> str:
    prefix, separator, raw_epoch = slug.rpartition("-")
    if not separator or not raw_epoch.isdigit() or count < 1:
        raise ValueError(f"invalid five-minute slug: {slug}")
    return f"{prefix}-{int(raw_epoch) - 300 * count}"


def slug_epoch(slug: str) -> int:
    raw = slug.rpartition("-")[2]
    if not raw.isdigit():
        raise ValueError(f"invalid five-minute slug: {slug}")
    return int(raw)


def bid_depth(book: OrderBookSnapshot, minimum_price: Decimal = Decimal("0")) -> Decimal:
    return sum(
        (level.size for level in book.bids if level.price >= minimum_price and level.size > 0),
        Decimal("0"),
    )


def ask_depth(book: OrderBookSnapshot, maximum_price: Decimal) -> Decimal:
    return sum(
        (level.size for level in book.asks if level.price <= maximum_price and level.size > 0),
        Decimal("0"),
    )


def market_health_from_books(
    *,
    trend_side: Direction,
    up_book: OrderBookSnapshot,
    down_book: OrderBookSnapshot,
    making_amount: Decimal,
    spot_prices: list[Decimal],
    open_price: Decimal,
    market_data_ok: bool = True,
    trading_api_ok: bool = True,
    short_volatility_override: Decimal | None = None,
    five_minute_volatility_override: Decimal | None = None,
) -> MarketHealth:
    trend_book = up_book if trend_side is Direction.UP else down_book
    quote = trend_book.quote
    spread = (
        quote.ask - quote.bid
        if quote.ask is not None and quote.bid is not None
        else Decimal("1")
    )
    if open_price <= 0:
        absolute_move = Decimal("1")
    else:
        latest = spot_prices[-1] if spot_prices else open_price
        absolute_move = abs(latest - open_price) / open_price
    returns = [
        abs(current - previous) / previous
        for previous, current in zip(spot_prices, spot_prices[1:])
        if previous > 0
    ]
    short_volatility = (
        short_volatility_override
        if short_volatility_override is not None
        else max(returns[-5:], default=Decimal("0"))
    )
    depth = bid_depth(trend_book)
    return MarketHealth(
        short_volatility=short_volatility,
        absolute_window_move=absolute_move,
        trend_bid_depth=depth,
        trend_spread=spread,
        estimated_sellable=quote.bid is not None and depth >= making_amount,
        market_data_ok=market_data_ok,
        trading_api_ok=trading_api_ok,
        five_minute_volatility=(
            five_minute_volatility_override
            if five_minute_volatility_override is not None
            else Decimal("0")
        ),
    )


class ReversalRuntime:
    """Crash-safe V1.1 window orchestration around the pure strategy state machine."""

    def __init__(
        self,
        *,
        strategy: ReversalV11,
        state_path: Path,
        winner_lookup: Callable[[str], str | None],
        splitter: Splitter | None,
        trader: ExitTrader | None,
        signature_type: int,
        live: bool,
        execution_mode: str = "split_sell",
        order_callback: Callable[[dict[str, Any]], None] | None = None,
        chain_winner_lookup: Callable[[str], str | None] | None = None,
        unlocked_profit_lookup: Callable[[Decimal], Decimal] | None = None,
    ) -> None:
        self.strategy = strategy
        self.state_path = state_path
        self.winner_lookup = winner_lookup
        self.splitter = splitter
        self.trader = trader
        self.signature_type = signature_type
        self.live = live
        if execution_mode not in {"split_sell", "direct_buy"}:
            raise ValueError("execution_mode must be split_sell or direct_buy")
        self.execution_mode = execution_mode
        self.order_callback = order_callback
        self.chain_winner_lookup = chain_winner_lookup
        self.unlocked_profit_lookup = unlocked_profit_lookup
        self._next_chain_verification_at = 0.0
        self._next_chain_backfill_at = 0.0
        self._next_gamma_verification_at = 0.0
        self._next_gamma_backfill_at = 0.0
        self._historical_audit_worker: HistoricalAuditWorker | None = None
        self._historical_audits_inflight: set[tuple[str, str]] = set()
        self._historical_audit_retry_at: dict[tuple[str, str], float] = {}

    def save(self) -> None:
        self.strategy.dump(self.state_path)

    def set_boundary_price_mode(self, mode: str) -> bool:
        """Switch boundary conventions without ever mixing cached price types."""
        if mode == "twap30":
            mode = "twap60"
        if mode not in {
            "legacy",
            "twap60",
            "twap60_ptb_open",
            "twap_official_boundary_v2",
            "twap60_chainlink_boundary_v3",
        }:
            raise ValueError(
                "boundary price mode must be legacy, twap60, "
                "twap60_ptb_open, twap_official_boundary_v2, or "
                "twap60_chainlink_boundary_v3"
            )
        state = self.strategy.state
        if state.chainlink_price_mode == mode:
            return False
        state.chainlink_price_mode = mode
        state.chainlink_open_prices = {}
        self.save()
        return True

    def pump_historical_audits(
        self,
        *,
        max_candidates: int = 2,
        worker_time_budget_seconds: float = 2.0,
        apply_time_budget_seconds: float = 0.01,
    ) -> list[tuple[str, str, str]]:
        """Apply finished audits and schedule a bounded, nonblocking batch."""
        if self._historical_audit_worker is None:
            self._historical_audit_worker = HistoricalAuditWorker(
                gamma_lookup=self.winner_lookup,
                chain_lookup=self.chain_winner_lookup,
            )
        worker = self._historical_audit_worker
        max_candidates = max(0, max_candidates)
        apply_deadline = time.monotonic() + max(0.0, apply_time_budget_seconds)
        verified: list[tuple[str, str, str]] = []
        changed = False
        while time.monotonic() <= apply_deadline:
            try:
                result = worker.results.get_nowait()
            except queue.Empty:
                break
            self._historical_audits_inflight.discard((result.source, result.slug))
            if result.error:
                self._historical_audit_retry_at[(result.source, result.slug)] = (
                    time.monotonic() + (2.0 if result.source == "chain" else 15.0)
                )
                logger.warning(
                    "%s history audit pending for %s: %s",
                    result.source,
                    result.slug,
                    result.error,
                )
                continue
            if result.published not in {Direction.UP.value, Direction.DOWN.value}:
                self._historical_audit_retry_at[(result.source, result.slug)] = (
                    time.monotonic() + (2.0 if result.source == "chain" else 15.0)
                )
                continue
            self._historical_audit_retry_at.pop((result.source, result.slug), None)
            state = self.strategy.state
            expected = state.pending_gamma_results.get(result.slug)
            if expected is None:
                continue
            actual = Direction(result.published)
            if result.source == "chain":
                if actual is not expected:
                    raise ChainResultMismatch(
                        f"Polygon result mismatch for {result.slug}: "
                        f"provisional={expected.value} chain={actual.value}",
                        slug=result.slug,
                        provisional=expected,
                        official=actual,
                    )
                if result.slug not in state.chain_verified_slugs:
                    state.chain_verified_slugs.append(result.slug)
                    changed = True
            else:
                if actual is not expected:
                    if result.slug in state.chain_verified_slugs:
                        logger.error(
                            "Gamma audit conflicts with Polygon result for %s: "
                            "chain=%s gamma=%s",
                            result.slug,
                            expected.value,
                            actual.value,
                        )
                        del state.pending_gamma_results[result.slug]
                        state.chain_verified_slugs = [
                            slug
                            for slug in state.chain_verified_slugs
                            if slug != result.slug
                        ]
                        state.gamma_mismatch_slugs = (
                            state.gamma_mismatch_slugs + [result.slug]
                        )[-100:]
                        changed = True
                        continue
                    raise GammaResultMismatch(
                        f"Gamma result mismatch for {result.slug}: "
                        f"provisional={expected.value} Gamma={actual.value}",
                        slug=result.slug,
                        provisional=expected,
                        official=actual,
                    )
                del state.pending_gamma_results[result.slug]
                state.chain_verified_slugs = [
                    slug for slug in state.chain_verified_slugs if slug != result.slug
                ]
                state.gamma_verified_slugs = (
                    state.gamma_verified_slugs + [result.slug]
                )[-100:]
                changed = True
            verified.append((result.source, result.slug, actual.value))
        if changed:
            self.save()

        available_slots = max_candidates - len(self._historical_audits_inflight)
        if available_slots <= 0:
            return verified
        state = self.strategy.state
        deadline = time.monotonic() + max(0.0, worker_time_budget_seconds)
        now = time.monotonic()
        chain_candidates: list[tuple[str, str, Direction]] = []
        if self.chain_winner_lookup is not None:
            chain_candidates.extend(
                ("chain", slug, state.pending_gamma_results[slug])
                for slug in sorted(
                    state.pending_gamma_results,
                    key=slug_epoch,
                    reverse=True,
                )
                if slug not in state.chain_verified_slugs
                and ("chain", slug) not in self._historical_audits_inflight
                and now >= self._historical_audit_retry_at.get(("chain", slug), 0.0)
            )
        gamma_candidates = [
            ("gamma", slug, state.pending_gamma_results[slug])
            for slug in sorted(
                state.pending_gamma_results,
                key=slug_epoch,
                reverse=True,
            )
            if ("gamma", slug) not in self._historical_audits_inflight
            and now >= self._historical_audit_retry_at.get(("gamma", slug), 0.0)
        ]
        candidates: list[tuple[str, str, Direction]] = []
        for index in range(max(len(chain_candidates), len(gamma_candidates))):
            if index < len(chain_candidates):
                candidates.append(chain_candidates[index])
            if index < len(gamma_candidates):
                candidates.append(gamma_candidates[index])
        for source, slug, expected in candidates[:available_slots]:
            key = (source, slug)
            self._historical_audits_inflight.add(key)
            worker.submit(
                HistoricalAuditTask(
                    source=source,
                    slug=slug,
                    expected=expected,
                    deadline=deadline,
                )
            )
        return verified

    def send_daily_report_once(
        self,
        report_day: date,
        sender: Callable[[str], bool],
    ) -> bool:
        if self.daily_report_was_sent(report_day):
            return False
        if not sender(self.daily_report_text(report_day)):
            return False
        self.mark_daily_report_sent(report_day)
        return True

    def daily_report_was_sent(self, report_day: date) -> bool:
        return report_day.isoformat() in self.strategy.state.reported_days

    def daily_report_text(self, report_day: date) -> str:
        return format_daily_report(
            report_day,
            self.strategy.metrics(report_day),
            self.strategy.settings.attempt_limit,
        )

    def mark_daily_report_sent(self, report_day: date) -> None:
        key = report_day.isoformat()
        if key in self.strategy.state.reported_days:
            return
        self.strategy.state.reported_days = (
            self.strategy.state.reported_days + [key]
        )[-14:]
        self.save()

    def observe_chainlink_open_prices(
        self,
        open_prices: dict[str, Decimal],
    ) -> list[tuple[str, str]]:
        """Settle completed windows from consecutive official Chainlink boundaries."""
        changed = False
        for slug, price in open_prices.items():
            slug_epoch(slug)
            if price <= 0:
                raise ReversalRuntimeError(
                    f"invalid Chainlink open price for {slug}: {price}"
                )
            existing = self.strategy.state.chainlink_open_prices.get(slug)
            if existing is not None and existing != price:
                raise ReversalRuntimeError(
                    f"Chainlink open price changed for {slug}: {existing} -> {price}"
                )
            if existing is None:
                self.strategy.state.chainlink_open_prices[slug] = price
                changed = True

        ordered = sorted(
            self.strategy.state.chainlink_open_prices,
            key=slug_epoch,
        )
        observed: list[tuple[str, str]] = []
        last_epoch = (
            slug_epoch(self.strategy.state.last_settled_slug)
            if self.strategy.state.last_settled_slug
            else None
        )
        for opening_slug, closing_slug in zip(ordered, ordered[1:]):
            opening_epoch = slug_epoch(opening_slug)
            closing_epoch = slug_epoch(closing_slug)
            if closing_epoch != opening_epoch + 300:
                continue
            if last_epoch is not None and opening_epoch <= last_epoch:
                continue
            opening_price = self.strategy.state.chainlink_open_prices[opening_slug]
            closing_price = self.strategy.state.chainlink_open_prices[closing_slug]
            result = Direction(resolve_btc_5m_twap(opening_price, closing_price))
            outcome = self.strategy.settle_window(opening_slug, result)
            self.strategy.state.pending_gamma_results[opening_slug] = result
            observed.append((opening_slug, outcome))
            last_epoch = opening_epoch
            changed = True

        if ordered:
            keep = set(ordered[-6:])
            trimmed = {
                slug: price
                for slug, price in self.strategy.state.chainlink_open_prices.items()
                if slug in keep
            }
            if trimmed != self.strategy.state.chainlink_open_prices:
                self.strategy.state.chainlink_open_prices = trimmed
                changed = True
        if changed:
            self.save()
        return observed

    def record_chainlink_open_price(
        self,
        slug: str,
        price: Decimal,
        *,
        allow_correction: bool = False,
    ) -> None:
        """Persist one window's same-source Chainlink resolution boundary.

        Settlement remains explicit in ``observe_completed_window_prices`` so
        recording one boundary can never mutate round state by itself.
        """
        slug_epoch(slug)
        if price <= 0:
            raise ReversalRuntimeError(
                f"invalid Chainlink open price for {slug}: {price}"
            )
        existing = self.strategy.state.chainlink_open_prices.get(slug)
        if existing is not None and existing != price and not allow_correction:
            raise ReversalRuntimeError(
                f"Chainlink open price changed for {slug}: {existing} -> {price}"
            )
        if existing == price:
            return
        self.strategy.state.chainlink_open_prices[slug] = price
        ordered = sorted(
            self.strategy.state.chainlink_open_prices,
            key=slug_epoch,
        )
        keep = set(ordered[-6:])
        self.strategy.state.chainlink_open_prices = {
            value_slug: value_price
            for value_slug, value_price in self.strategy.state.chainlink_open_prices.items()
            if value_slug in keep
        }
        self.save()

    def observe_completed_window_prices(
        self,
        completed_prices: dict[str, tuple[Decimal, Decimal]],
    ) -> list[tuple[str, str]]:
        """Settle windows from completed same-source opening/closing boundaries."""
        completed_results: dict[str, Direction] = {}
        for slug, (opening_price, closing_price) in completed_prices.items():
            if opening_price <= 0 or closing_price <= 0:
                raise ReversalRuntimeError(
                    f"invalid completed price for {slug}: "
                    f"{opening_price} -> {closing_price}"
                )
            completed_results[slug] = Direction(
                resolve_btc_5m_twap(opening_price, closing_price)
            )
        return self.observe_completed_window_results(completed_results)

    def observe_completed_window_results(
        self,
        completed_results: dict[str, Direction],
    ) -> list[tuple[str, str]]:
        """Settle explicit provisional outcomes and queue Gamma verification."""
        observed: list[tuple[str, str]] = []
        last_epoch = (
            slug_epoch(self.strategy.state.last_settled_slug)
            if self.strategy.state.last_settled_slug
            else None
        )
        for slug in sorted(completed_results, key=slug_epoch):
            epoch = slug_epoch(slug)
            if last_epoch is not None and epoch <= last_epoch:
                continue
            result = completed_results[slug]
            expected = self.strategy.state.pending_gamma_results.get(slug)
            if expected is not None and expected is not result:
                raise GammaResultMismatch(
                    f"completed price mismatch for {slug}: provisional={expected.value} "
                    f"completed={result.value}",
                    slug=slug,
                    provisional=expected,
                    official=result,
                )
            outcome = self.strategy.settle_window(slug, result)
            self.strategy.state.pending_gamma_results[slug] = result
            observed.append((slug, outcome))
            last_epoch = epoch
        if observed:
            self.save()
        return observed

    def verify_gamma_results(self) -> list[tuple[str, str]]:
        """Verify Chainlink-derived results after Gamma publishes final 1/0 outcomes."""
        now = time.monotonic()
        if now < self._next_gamma_verification_at:
            return []
        self._next_gamma_verification_at = now + 15.0
        verified: list[tuple[str, str]] = []
        changed = False
        for slug in sorted(
            self.strategy.state.pending_gamma_results,
            key=slug_epoch,
        ):
            expected = self.strategy.state.pending_gamma_results[slug]
            try:
                published = self.winner_lookup(slug)
            except Exception as exc:
                logger.warning("Gamma verification pending for %s: %s", slug, exc)
                continue
            if published not in {Direction.UP.value, Direction.DOWN.value}:
                continue
            actual = Direction(published)
            if actual is not expected:
                if slug in self.strategy.state.chain_verified_slugs:
                    logger.error(
                        "Gamma audit conflicts with Polygon result for %s: chain=%s gamma=%s",
                        slug,
                        expected.value,
                        actual.value,
                    )
                    del self.strategy.state.pending_gamma_results[slug]
                    self.strategy.state.gamma_mismatch_slugs = (
                        self.strategy.state.gamma_mismatch_slugs + [slug]
                    )[-100:]
                    changed = True
                    continue
                raise GammaResultMismatch(
                    f"Gamma result mismatch for {slug}: Chainlink={expected.value} "
                    f"Gamma={actual.value}",
                    slug=slug,
                    provisional=expected,
                    official=actual,
                )
            del self.strategy.state.pending_gamma_results[slug]
            self.strategy.state.gamma_verified_slugs = (
                self.strategy.state.gamma_verified_slugs + [slug]
            )[-100:]
            verified.append((slug, actual.value))
            changed = True
        if changed:
            self.save()
        return verified

    def verify_chain_results(self) -> list[tuple[str, str]]:
        """Check Polygon payouts before Gamma; keep Gamma pending for later audit."""
        if self.chain_winner_lookup is None:
            return []
        now = time.monotonic()
        if now < self._next_chain_verification_at:
            return []
        self._next_chain_verification_at = now + 2.0
        verified: list[tuple[str, str]] = []
        state = self.strategy.state
        for slug in sorted(state.pending_gamma_results, key=slug_epoch):
            if slug in state.chain_verified_slugs:
                continue
            expected = state.pending_gamma_results[slug]
            try:
                published = self.chain_winner_lookup(slug)
            except Exception as exc:
                logger.warning("Polygon resolution pending for %s: %s", slug, exc)
                continue
            if published not in {Direction.UP.value, Direction.DOWN.value}:
                continue
            actual = Direction(published)
            if actual is not expected:
                raise ChainResultMismatch(
                    f"Polygon result mismatch for {slug}: provisional={expected.value} "
                    f"chain={actual.value}",
                    slug=slug,
                    provisional=expected,
                    official=actual,
                )
            state.chain_verified_slugs = (state.chain_verified_slugs + [slug])[-100:]
            verified.append((slug, actual.value))
        if verified:
            self.save()
        return verified

    def backfill_immediate_chain_results(
        self,
        current_slug: str,
    ) -> list[tuple[str, str]]:
        """Fill an exact missing trigger sequence from finalized Polygon payouts."""
        if self.chain_winner_lookup is None:
            return []
        state = self.strategy.state
        if state.active_round is not None or state.prepared_split is not None:
            return []
        count = self.strategy.settings.trigger_streak
        expected = [
            previous_5m_slug(current_slug, offset)
            for offset in range(count, 0, -1)
        ]
        if state.recent_slugs[-count:] == expected:
            return []
        known = {
            slug: result
            for slug, result in zip(state.recent_slugs, state.recent_results)
            if slug in expected
        }
        missing = [slug for slug in expected if slug not in known]
        if not missing:
            return []
        now = time.monotonic()
        if now < self._next_chain_backfill_at:
            return []
        self._next_chain_backfill_at = now + 2.0
        backfilled: list[tuple[str, str]] = []
        for slug in missing:
            try:
                published = self.chain_winner_lookup(slug)
            except Exception as exc:
                logger.warning("Polygon history backfill pending for %s: %s", slug, exc)
                return []
            if published not in {Direction.UP.value, Direction.DOWN.value}:
                return []
            official = Direction(published)
            known[slug] = official
            backfilled.append((slug, official.value))
        if any(slug not in known for slug in expected):
            return []
        for slug, result in backfilled:
            state.pending_gamma_results[slug] = Direction(result)
            state.chain_verified_slugs = (state.chain_verified_slugs + [slug])[-100:]
        state.recent_slugs = expected
        state.recent_results = [known[slug] for slug in expected]
        final_side = state.recent_results[-1]
        state.current_streak_side = final_side
        streak = 0
        for result in reversed(state.recent_results):
            if result is not final_side:
                break
            streak += 1
        state.current_streak = streak
        state.last_settled_slug = expected[-1]
        self.save()
        return backfilled

    def quarantine_chain_mismatch(self, mismatch: ChainResultMismatch) -> None:
        """Apply the Polygon payout while retaining it for Gamma audit."""
        state = self.strategy.state
        if mismatch.slug in state.recent_slugs:
            index = state.recent_slugs.index(mismatch.slug)
            state.recent_results[index] = mismatch.official
            final_side = state.recent_results[-1]
            streak = 0
            for result in reversed(state.recent_results):
                if result is not final_side:
                    break
                streak += 1
            state.current_streak_side = final_side
            state.current_streak = streak
        state.pending_gamma_results[mismatch.slug] = mismatch.official
        state.chain_verified_slugs = (state.chain_verified_slugs + [mismatch.slug])[-100:]
        state.chain_mismatch_slugs = (state.chain_mismatch_slugs + [mismatch.slug])[-100:]
        self.strategy.metrics().api_order_errors += 1
        self.save()

    def backfill_immediate_gamma_results(
        self,
        current_slug: str,
    ) -> list[tuple[str, str]]:
        """Fill only missing trigger history from finalized Gamma outcomes.

        Chainlink TWAP remains the fast path.  Gamma is consulted only when the
        exact predecessor sequence has a hole, and winner_lookup only returns a
        side after Gamma publishes a final 1/0 outcome.
        """
        state = self.strategy.state
        if state.active_round is not None or state.prepared_split is not None:
            return []
        count = self.strategy.settings.trigger_streak
        expected = [
            previous_5m_slug(current_slug, offset)
            for offset in range(count, 0, -1)
        ]
        if state.recent_slugs[-count:] == expected:
            return []

        known = {
            slug: result
            for slug, result in zip(state.recent_slugs, state.recent_results)
            if slug in expected
        }
        missing = [slug for slug in expected if slug not in known]
        if not missing:
            return []
        now = time.monotonic()
        if now < self._next_gamma_backfill_at:
            return []
        self._next_gamma_backfill_at = now + 2.0

        backfilled: list[tuple[str, str]] = []
        for slug in missing:
            try:
                published = self.winner_lookup(slug)
            except Exception as exc:
                logger.warning("Gamma history backfill pending for %s: %s", slug, exc)
                return []
            if published not in {Direction.UP.value, Direction.DOWN.value}:
                return []
            official = Direction(published)
            known[slug] = official
            backfilled.append((slug, official.value))

        if any(slug not in known for slug in expected):
            return []
        state.recent_slugs = expected
        state.recent_results = [known[slug] for slug in expected]
        final_side = state.recent_results[-1]
        streak = 0
        for result in reversed(state.recent_results):
            if result is not final_side:
                break
            streak += 1
        state.current_streak_side = final_side
        state.current_streak = streak
        state.gamma_verified_slugs = (
            state.gamma_verified_slugs + [slug for slug, _ in backfilled]
        )[-100:]
        self.save()
        return backfilled

    def quarantine_gamma_mismatch(self, mismatch: GammaResultMismatch) -> None:
        """Apply Gamma's final side and retain the provisional conflict for audit."""
        state = self.strategy.state
        if mismatch.slug in state.recent_slugs:
            index = state.recent_slugs.index(mismatch.slug)
            state.recent_results[index] = mismatch.official
            final_side = state.recent_results[-1]
            streak = 0
            for result in reversed(state.recent_results):
                if result is not final_side:
                    break
                streak += 1
            state.current_streak_side = final_side
            state.current_streak = streak
        self.strategy.quarantine_gamma_mismatch(mismatch.slug)
        self.save()

    def correct_gamma_mismatch_position(
        self,
        *,
        market: Market,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
        seconds_left: Decimal,
        source: str = "gamma",
        allow_replacement: bool = True,
    ) -> ReversalTickResult:
        """Close a filled provisional order and replace it only without added loss.

        The caller must apply ``quarantine_gamma_mismatch`` first so recent
        history already reflects Gamma's final outcome.
        """
        state = self.strategy.state
        active = state.active_round
        if active is None or active.awaiting_window != market.slug:
            return ReversalTickResult("gamma_state_corrected_no_active_order")
        if self.execution_mode != "direct_buy" or self.trader is None:
            return ReversalTickResult(
                "gamma_correction_blocked",
                detail="automatic correction supports direct-buy mode only",
            )

        count = self.strategy.settings.trigger_streak
        immediate = (
            state.recent_slugs[-count:]
            == [previous_5m_slug(market.slug, offset) for offset in range(count, 0, -1)]
        )
        corrected_trend = (
            state.recent_results[-1]
            if immediate
            and len(state.recent_results) >= count
            and len(set(state.recent_results[-count:])) == 1
            else None
        )
        if corrected_trend is active.trend_side:
            return ReversalTickResult("gamma_state_corrected_order_unchanged")

        # FAK has no live remainder to cancel.  Before any fill, change the
        # persisted plan so the next retry uses Gamma's corrected direction.
        if active.exit_sold_shares <= Decimal("0.000001"):
            if not allow_replacement:
                state.active_round = None
                state.last_opening_processed_slug = market.slug
                self.save()
                return ReversalTickResult(f"{source}_unfilled_plan_cancelled")
            if corrected_trend is None:
                state.active_round = None
                state.last_opening_processed_slug = market.slug
                self.save()
                return ReversalTickResult("gamma_invalid_trigger_cancelled")
            active.trend_side = corrected_trend
            active.execution_phase = "direct_entry_ready"
            active.entry_submitted_price = None
            self.save()
            return ReversalTickResult(
                "gamma_unfilled_order_replaced",
                detail=f"new_side={active.target_side.value}",
            )

        if allow_replacement and seconds_left < GAMMA_CORRECTION_MIN_SECONDS_LEFT:
            return ReversalTickResult(
                "gamma_correction_blocked",
                detail=f"seconds_left={seconds_left} below 30",
            )

        wrong_side = active.target_side
        wrong_index = 0 if wrong_side is Direction.UP else 1
        wrong_token = market.token_ids[wrong_index]
        wrong_book = up_book if wrong_index == 0 else down_book
        wrong_bid = wrong_book.quote.bid
        available = self.trader.conditional_balance(wrong_token, self.signature_type)
        wrong_shares = min(active.exit_sold_shares, available)
        depth = bid_depth(wrong_book, wrong_bid) if wrong_bid is not None else Decimal("0")
        if not allow_replacement:
            wrong_shares = min(wrong_shares, depth)
        if (
            wrong_bid is None
            or wrong_bid <= 0
            or wrong_shares <= Decimal("0.000001")
            or (allow_replacement and depth < wrong_shares)
        ):
            return ReversalTickResult(
                "gamma_correction_blocked",
                detail="wrong-side position has no immediately sellable best-bid depth",
            )

        corrected_side = corrected_trend.opposite if corrected_trend is not None else None
        corrected_book = (
            up_book if corrected_side is Direction.UP else down_book
            if corrected_side is Direction.DOWN
            else None
        )
        corrected_ask = corrected_book.quote.ask if corrected_book is not None else None
        if corrected_side is not None and (
            corrected_ask is None
            or corrected_ask <= 0
            or corrected_ask > GAMMA_CORRECTION_MAX_ASK
        ):
            return ReversalTickResult(
                "gamma_correction_blocked",
                detail=f"corrected ask {corrected_ask} exceeds 0.80 or is unavailable",
            )

        sell_response = self.trader.sell_limit(
            wrong_token,
            wrong_bid,
            wrong_shares,
            market.minimum_tick_size,
            market.neg_risk,
            "FAK",
        )
        sold_shares, sell_proceeds = _sell_fill_amounts(sell_response)
        sell_fee = (
            sold_shares
            * Decimal("0.07")
            * wrong_bid
            * (Decimal("1") - wrong_bid)
        )
        sell_order = {
            "slug": market.slug,
            "side": wrong_side.value,
            "token_id": wrong_token,
            "price": str(wrong_bid),
            "size": str(wrong_shares),
            "order_type": "FAK",
            "order_role": f"{source}_correction_exit",
            "round_id": active.round_id,
            "attempt": active.failures + 1,
            "response": sell_response,
        }
        if sold_shares <= Decimal("0.000001"):
            return ReversalTickResult(
                "gamma_correction_exit_unmatched",
                order=sell_order,
            )
        if self.order_callback is not None:
            self.order_callback(sell_order)
        metrics = self.strategy.metrics()
        metrics.sell_proceeds += sell_proceeds
        metrics.fees += sell_fee
        metrics.net_profit = (
            metrics.sell_proceeds
            + metrics.settlement_payout
            - metrics.total_making_amount
            - metrics.fees
            - metrics.slippage
        )
        remaining_wrong = max(Decimal("0"), active.exit_sold_shares - sold_shares)
        if remaining_wrong > Decimal("0.000001"):
            original_shares = active.exit_sold_shares
            active.exit_sold_shares = remaining_wrong
            active.exit_sell_proceeds *= remaining_wrong / original_shares
            active.planned_shares = remaining_wrong
            self.save()
            return ReversalTickResult(
                "gamma_correction_exit_partial",
                order=sell_order,
                detail=f"remaining_wrong_shares={remaining_wrong}",
            )

        original_round_id = active.round_id
        original_failures = active.failures
        original_committed = active.committed
        original_cumulative_loss = active.cumulative_loss
        original_soft_limit_final_recovery = active.soft_limit_final_recovery
        state.active_round = None
        state.last_opening_processed_slug = market.slug
        if corrected_side is None or not allow_replacement:
            self.save()
            return ReversalTickResult(
                f"{source}_position_closed",
                order=sell_order,
            )

        assert corrected_ask is not None
        net_sell_budget = max(Decimal("0"), sell_proceeds - sell_fee)
        buy_fee_per_share = (
            Decimal("0.07") * corrected_ask * (Decimal("1") - corrected_ask)
        )
        replacement_shares = (
            net_sell_budget / (corrected_ask + buy_fee_per_share)
        ).quantize(ORDER_SIZE_QUANTUM, rounding=ROUND_DOWN)
        if (
            replacement_shares <= 0
            or replacement_shares * corrected_ask < MIN_MARKETABLE_BUY_NOTIONAL
        ):
            self.save()
            return ReversalTickResult(
                "gamma_replacement_unfunded_position_closed",
                order=sell_order,
            )
        corrected_index = 0 if corrected_side is Direction.UP else 1
        corrected_token = market.token_ids[corrected_index]
        buy_response = self.trader.buy_limit(
            corrected_token,
            corrected_ask,
            replacement_shares,
            market.minimum_tick_size,
            market.neg_risk,
            "FAK",
        )
        buy_cost, bought_shares = _buy_fill_amounts(buy_response)
        buy_order = {
            "slug": market.slug,
            "side": corrected_side.value,
            "token_id": corrected_token,
            "price": str(corrected_ask),
            "size": str(replacement_shares),
            "order_type": "FAK",
            "order_role": f"{source}_correction_replacement",
            "round_id": original_round_id,
            "attempt": original_failures + 1,
            "response": buy_response,
        }
        if bought_shares <= Decimal("0.000001"):
            self.save()
            return ReversalTickResult(
                "gamma_replacement_unmatched_position_closed",
                order=buy_order,
            )
        buy_fee = (
            bought_shares
            * Decimal("0.07")
            * corrected_ask
            * (Decimal("1") - corrected_ask)
        )
        state.active_round = ActiveRound(
            round_id=original_round_id,
            trend_side=corrected_trend,
            failures=original_failures,
            awaiting_window=market.slug,
            committed=original_committed,
            execution_phase="direct_entry_complete",
            split_transaction_hash="direct-buy",
            exit_sold_shares=bought_shares,
            exit_sell_proceeds=buy_cost,
            planned_shares=bought_shares,
            entry_fees=buy_fee,
            cumulative_loss=original_cumulative_loss,
            soft_limit_final_recovery=original_soft_limit_final_recovery,
        )
        metrics.total_making_amount += buy_cost
        metrics.fees += buy_fee
        metrics.net_profit = (
            metrics.sell_proceeds
            + metrics.settlement_payout
            - metrics.total_making_amount
            - metrics.fees
            - metrics.slippage
        )
        self.save()
        if self.order_callback is not None:
            self.order_callback(buy_order)
        return ReversalTickResult(
            "gamma_order_replaced",
            order=buy_order,
            detail=(
                f"closed={wrong_side.value} {sold_shares} replaced={corrected_side.value} "
                f"{bought_shares} without increasing max-loss budget"
            ),
        )

    def sync_settlements(self, current_slug: str) -> list[tuple[str, str]]:
        current_epoch = slug_epoch(current_slug)
        candidates = {
            previous_5m_slug(current_slug, 2),
            previous_5m_slug(current_slug, 1),
        }
        active = self.strategy.state.active_round
        if active is not None and active.awaiting_window is not None:
            if slug_epoch(active.awaiting_window) < current_epoch:
                candidates.add(active.awaiting_window)
        last_epoch = (
            slug_epoch(self.strategy.state.last_settled_slug)
            if self.strategy.state.last_settled_slug
            else None
        )
        observed: list[tuple[str, str]] = []
        for candidate in sorted(candidates, key=slug_epoch):
            candidate_epoch = slug_epoch(candidate)
            if candidate_epoch >= current_epoch:
                continue
            if last_epoch is not None and candidate_epoch <= last_epoch:
                continue
            winner = self.winner_lookup(candidate)
            if winner not in {Direction.UP.value, Direction.DOWN.value}:
                break
            outcome = self.strategy.settle_window(candidate, winner)
            self.save()
            observed.append((candidate, outcome))
            last_epoch = candidate_epoch
        return observed

    def reconcile_stale_active_round(self) -> tuple[str, str] | None:
        """Resolve an old direct-buy round without replaying missed windows."""
        active = self.strategy.state.active_round
        last_slug = self.strategy.state.last_settled_slug
        if active is None or active.awaiting_window is None or last_slug is None:
            return None
        active_slug = active.awaiting_window
        if slug_epoch(active_slug) >= slug_epoch(last_slug):
            return None
        winner = self.winner_lookup(active_slug)
        if winner not in {Direction.UP.value, Direction.DOWN.value}:
            raise ReversalRuntimeError(
                f"stale active round result unavailable for {active_slug}"
            )
        outcome = self.strategy.reconcile_stale_direct_entry(
            active_slug,
            winner,
            day=date.fromtimestamp(slug_epoch(active_slug)),
        )
        self.save()
        return active_slug, outcome

    def tick(
        self,
        *,
        market: Market,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
        health: MarketHealth | None = None,
        health_by_side: dict[Direction, MarketHealth] | None = None,
        book_refresh: Callable[[], tuple[OrderBookSnapshot, OrderBookSnapshot]]
        | None = None,
        spot_price: Decimal | None = None,
        open_price: Decimal | None = None,
        probability_up: Decimal | None = None,
    ) -> ReversalTickResult:
        existing_active = self.strategy.state.active_round
        recovery_entry_retry = (
            existing_active is not None
            and existing_active.awaiting_window == market.slug
            and existing_active.execution_phase
            in {"direct_entry_ready", "direct_entry_partial"}
            and (
                self.strategy.settings.uses_dynamic_recovery(
                    existing_active.failures + 1
                )
                or self.strategy.settings.uses_full_loss_recovery(
                    existing_active.failures + 1
                )
            )
        )
        if (
            existing_active is not None
            and existing_active.awaiting_window == market.slug
            and existing_active.execution_phase
            in {
                "split_confirmed",
                "trend_exit_partial",
                "trend_exit_submitting",
                "trend_exit_complete",
                "direct_entry_ready",
                "direct_entry_partial",
                "direct_entry_submitting",
                "direct_entry_complete",
            }
            and not recovery_entry_retry
        ):
            selected = (
                health_by_side.get(existing_active.trend_side)
                if health_by_side is not None
                else health
            )
            if selected is None:
                raise ReversalRuntimeError("no market-health snapshot for active trend side")
            resumed_plan = self.strategy.plan_window(market.slug, selected)
            assert resumed_plan is not None
            if book_refresh is not None:
                up_book, down_book = book_refresh()
            return self._finish_selected_plan(
                market=market,
                up_book=up_book,
                down_book=down_book,
                plan=resumed_plan,
            )
        if (
            self.strategy.state.prepared_split is None
            and self.strategy.state.last_opening_processed_slug == market.slug
            and not recovery_entry_retry
        ):
            return ReversalTickResult("opening_already_processed")
        if not self._immediate_results_available(market.slug):
            count = self.strategy.settings.trigger_streak
            return ReversalTickResult(
                "waiting_chainlink_boundary_no_split",
                detail=(
                    f"{count} immediate Chainlink-derived results are not available"
                ),
            )

        trend_side = self._confirmed_trend_side()
        prepared = self.strategy.state.prepared_split
        if trend_side is None and prepared is None:
            count = self.strategy.settings.trigger_streak
            self.strategy.state.last_opening_processed_slug = market.slug
            self.save()
            return ReversalTickResult(
                "no_trigger_no_split",
                detail=f"last {count} settled windows are not all the same direction",
            )
        selected_health = (
            health_by_side.get(trend_side)
            if health_by_side is not None and trend_side is not None
            else health
        )
        if selected_health is None and health_by_side:
            selected_health = next(iter(health_by_side.values()))
        if selected_health is None:
            raise ReversalRuntimeError("no market-health snapshot for confirmed trend side")
        plan = self.strategy.plan_window(market.slug, selected_health)
        if plan is None:
            if prepared is None:
                self.strategy.state.last_opening_processed_slug = market.slug
                self.save()
                volatility_reason = (
                    self.strategy.settings.first_stage_volatility_block_reason(
                        selected_health.short_volatility,
                        selected_health.five_minute_volatility,
                    )
                )
                if volatility_reason is not None:
                    return ReversalTickResult(
                        "first_stage_extreme_volatility_no_split",
                        detail=(
                            f"reason={volatility_reason}; "
                            f"RV60={selected_health.short_volatility} "
                            f"threshold={self.strategy.settings.first_stage_max_rv60}; "
                            f"RV300={selected_health.five_minute_volatility} "
                            f"base_threshold={self.strategy.settings.first_stage_max_rv300} "
                            f"persistence_ratio="
                            f"{self.strategy.settings.first_stage_rv300_persistence_ratio} "
                            f"hard_multiplier="
                            f"{self.strategy.settings.first_stage_rv300_hard_multiplier}"
                        ),
                    )
                return ReversalTickResult(
                    "trigger_filtered_no_split",
                    detail="market-health filter blocked entry",
                )
            return self._merge_unused_opening_split(market, prepared.amount)

        if self.execution_mode == "direct_buy" and prepared is None:
            active = self.strategy.state.active_round
            assert active is not None
            if (
                self.strategy.settings.sparse_recovery_notional_stages
                and plan.making_amount == 0
                and active.execution_phase == "planned"
            ):
                self.strategy.mark_observation_stage(plan)
                self.save()
                return ReversalTickResult(
                    "sparse_stage_observing",
                    plan=plan,
                    detail=f"stage {plan.attempt} is observation-only; no order submitted",
                )
            if active.execution_phase in {
                "planned",
                "direct_entry_ready",
                "direct_entry_partial",
            }:
                if self.strategy.settings.sparse_recovery_notional_stages:
                    retained_book = (
                        up_book if plan.retained_side is Direction.UP else down_book
                    )
                    ask = retained_book.quote.ask
                    if ask is None or ask <= 0 or ask >= 1:
                        return ReversalTickResult(
                            "entry_book_pending",
                            plan=plan,
                            detail="retained-side ask unavailable for sparse-stage sizing",
                        )
                    if active.execution_phase == "planned":
                        cash_floor = self.strategy.settings.sparse_recovery_notional_stages[
                            plan.attempt - 1
                        ]
                        floor_shares = _affordable_marketable_buy_size(
                            available_collateral=cash_floor,
                            price=ask,
                        )
                        if (
                            floor_shares <= 0
                            and cash_floor < MIN_MARKETABLE_BUY_NOTIONAL
                        ):
                            floor_shares = _marketable_buy_size(
                                nominal_shares=cash_floor / ask,
                                price=ask,
                            )
                        if floor_shares <= 0:
                            self._defer_or_abandon_unfilled_attempt(plan)
                            self.save()
                            return ReversalTickResult(
                                "sparse_entry_filtered",
                                plan=plan,
                                detail="stage budget is below the exchange minimum",
                            )
                        desired_shares = floor_shares
                        if self.strategy.settings.uses_sparse_full_loss_recovery(
                            plan.attempt
                        ):
                            desired_shares = full_loss_recovery_size(
                                cumulative_loss=active.cumulative_loss,
                                entry_price=ask,
                                recovery_fraction=Decimal("1"),
                                minimum_profit=Decimal("0"),
                                minimum_shares=floor_shares,
                            )
                        available_collateral = self.trader.collateral_balance(
                            self.signature_type
                        )
                        required = desired_shares * ask
                        if required > available_collateral:
                            self.strategy.abandon_filtered_attempt(plan)
                            self.save()
                            return ReversalTickResult(
                                "break_even_target_unfunded",
                                plan=plan,
                                detail=(
                                    f"required {required} pUSD for stage floor/recovery; "
                                    f"available {available_collateral}"
                                ),
                            )
                        plan = self.strategy.resize_active_plan(plan, desired_shares)
                        self.save()
                elif self.strategy.settings.fixed_notional_stages:
                    retained_book = (
                        up_book if plan.retained_side is Direction.UP else down_book
                    )
                    ask = retained_book.quote.ask
                    max_ask = self.strategy.settings.fixed_notional_max_ask
                    recovery_attempt = (
                        self.strategy.settings.uses_fixed_notional_full_loss_recovery(
                            plan.attempt
                        )
                    )
                    if (
                        ask is None
                        or ask <= 0
                        or ask >= 1
                        or (not recovery_attempt and ask >= max_ask)
                    ):
                        if active.exit_sold_shares <= 0:
                            self._defer_or_abandon_unfilled_attempt(plan)
                        self.save()
                        return ReversalTickResult(
                            "fixed_notional_entry_filtered",
                            plan=plan,
                            detail=(
                                "retained-side ask unavailable"
                                if ask is None
                                else f"ask {ask} is not below strict limit {max_ask}"
                                if not recovery_attempt
                                else f"invalid recovery ask {ask}"
                            ),
                        )
                    if active.execution_phase == "planned":
                        cash_budget = self.strategy.settings.fixed_notional_stages[
                            plan.attempt - 1
                        ]
                        fixed_shares = _affordable_marketable_buy_size(
                            available_collateral=cash_budget,
                            price=ask,
                        )
                        if (
                            fixed_shares <= 0
                            and cash_budget < MIN_MARKETABLE_BUY_NOTIONAL
                        ):
                            # A configured 1 pUSD stage is nominal: Polymarket
                            # rejects marketable BUY orders below 1.01 pUSD and
                            # requires two-decimal maker precision. Submit the
                            # smallest exchange-valid order for that price so
                            # the 1 pUSD stages are executable instead of being
                            # silently skipped forever.
                            fixed_shares = _marketable_buy_size(
                                nominal_shares=cash_budget / ask,
                                price=ask,
                            )
                        if fixed_shares <= 0:
                            self._defer_or_abandon_unfilled_attempt(plan)
                            self.save()
                            return ReversalTickResult(
                                "fixed_notional_entry_filtered",
                                plan=plan,
                                detail="stage budget is below the exchange minimum",
                            )
                        desired_shares = fixed_shares
                        if recovery_attempt:
                            desired_shares = full_loss_recovery_size(
                                cumulative_loss=active.cumulative_loss,
                                entry_price=ask,
                                recovery_fraction=Decimal("1"),
                                minimum_profit=Decimal("0"),
                                minimum_shares=fixed_shares,
                            )
                            available_collateral = self.trader.collateral_balance(
                                self.signature_type
                            )
                            required = desired_shares * ask
                            if required > available_collateral:
                                self.strategy.abandon_filtered_attempt(plan)
                                self.save()
                                return ReversalTickResult(
                                    "break_even_target_unfunded",
                                    plan=plan,
                                    detail=(
                                        f"required {required} pUSD for stage floor/recovery; "
                                        f"available {available_collateral}"
                                    ),
                                )
                        plan = self.strategy.resize_active_plan(plan, desired_shares)
                        self.save()
                elif (
                    self.strategy.settings.uses_dynamic_recovery(plan.attempt)
                ):
                    retained_book = (
                        up_book if plan.retained_side is Direction.UP else down_book
                    )
                    ask = retained_book.quote.ask
                    retained_probability = (
                        probability_up
                        if plan.retained_side is Direction.UP
                        else Decimal("1") - probability_up
                        if probability_up is not None
                        else None
                    )
                    decision = (
                        dynamic_recovery_decision(
                            cumulative_loss=active.cumulative_loss,
                            entry_price=ask,
                            retained_side=plan.retained_side,
                            retained_probability=retained_probability,
                            spot_price=spot_price,
                            open_price=open_price,
                            settings=self.strategy.settings,
                        )
                        if ask is not None
                        else DynamicRecoveryDecision(False, reason="retained-side ask unavailable")
                    )
                    if not decision.allowed:
                        self.strategy.abandon_dynamic_recovery(plan)
                        self.save()
                        return ReversalTickResult(
                            "dynamic_recovery_skipped",
                            plan=plan,
                            detail=decision.reason,
                        )
                    if active.execution_phase == "planned":
                        plan = self.strategy.resize_active_plan(plan, decision.shares)
                        self.save()
                elif (
                    (
                        plan.attempt == 1
                        and self.strategy.settings.minimum_round_profit > 0
                    )
                    or self.strategy.settings.uses_full_loss_recovery(plan.attempt)
                ):
                    retained_book = (
                        up_book if plan.retained_side is Direction.UP else down_book
                    )
                    ask = retained_book.quote.ask
                    if ask is None:
                        return ReversalTickResult(
                            "entry_book_pending",
                            plan=plan,
                            detail="retained-side ask unavailable for full-loss sizing",
                        )
                    recovery_shares = full_loss_recovery_size(
                        cumulative_loss=active.cumulative_loss,
                        entry_price=ask,
                        recovery_fraction=(
                            Decimal("1")
                            if plan.attempt <= 4
                            else self.strategy.settings.late_stage_recovery_fraction
                        ),
                        minimum_profit=self.strategy.settings.minimum_round_profit,
                        minimum_shares=self.strategy.settings.stakes[
                            plan.attempt - 1
                        ],
                        filled_shares=active.exit_sold_shares,
                        filled_cost=active.exit_sell_proceeds,
                        filled_fees=active.entry_fees,
                    )
                    desired_additional = max(
                        Decimal("0"),
                        recovery_shares - active.exit_sold_shares,
                    )
                    available_collateral = self.trader.collateral_balance(
                        self.signature_type
                    )
                    if desired_additional * ask > available_collateral:
                        if (
                            self.strategy.settings.minimum_round_profit > 0
                            or self.strategy.settings.full_loss_recovery_strict_funding
                        ):
                            logger.warning(
                                "REVERSAL_PROFIT_TARGET_UNFUNDED slug=%s attempt=%s "
                                "desired_shares=%s required_notional=%s "
                                "available_collateral=%s ask=%s",
                                market.slug,
                                plan.attempt,
                                desired_additional,
                                desired_additional * ask,
                                available_collateral,
                                ask,
                            )
                            if (
                                active.execution_phase == "planned"
                                and active.exit_sold_shares <= 0
                            ):
                                self.strategy.abandon_filtered_attempt(plan)
                                self.save()
                                return ReversalTickResult(
                                    (
                                        "profit_target_unfunded"
                                        if self.strategy.settings.minimum_round_profit > 0
                                        else "break_even_target_unfunded"
                                    ),
                                    plan=plan,
                                    detail=(
                                        f"required {desired_additional * ask} pUSD to cover "
                                        "the round break-even target; "
                                        f"available {available_collateral}"
                                    ),
                                )
                            return ReversalTickResult(
                                "entry_balance_insufficient",
                                plan=plan,
                                detail=(
                                    f"partial entry requires {desired_additional * ask} "
                                    "pUSD more to preserve the round break-even "
                                    "target; available "
                                    f"{available_collateral}"
                                ),
                            )
                        affordable = _affordable_marketable_buy_size(
                            available_collateral=available_collateral,
                            price=ask,
                        )
                        if affordable <= 0:
                            return ReversalTickResult(
                                "entry_balance_insufficient",
                                plan=plan,
                                detail=(
                                    f"available {available_collateral} pUSD is below the "
                                    "exchange-valid marketable BUY minimum"
                                ),
                            )
                        recovery_shares = active.exit_sold_shares + affordable
                    if recovery_shares != plan.making_amount:
                        plan = self.strategy.resize_active_plan(plan, recovery_shares)
                        self.save()
                retained_book = (
                    up_book if plan.retained_side is Direction.UP else down_book
                )
                round_loss_block = self._round_loss_entry_block_reason(
                    plan=plan,
                    book=retained_book,
                )
                if round_loss_block is not None:
                    if active.exit_sold_shares <= 0:
                        self._defer_or_abandon_unfilled_attempt(plan)
                    self.save()
                    return ReversalTickResult(
                        "round_loss_limit_reached",
                        plan=plan,
                        detail=round_loss_block,
                    )
                if self.strategy.settings.compact_two_stage_enabled:
                    compact_block = self._compact_entry_block_reason(
                        plan=plan,
                        book=retained_book,
                    )
                    if compact_block is not None:
                        if active.exit_sold_shares <= 0:
                            self._defer_or_abandon_unfilled_attempt(plan)
                        self.save()
                        return ReversalTickResult(
                            "compact_entry_filtered",
                            plan=plan,
                            detail=compact_block,
                        )
                if self.strategy.settings.uses_first_stage_order_rules(plan.attempt):
                    first_stage_block = self._first_stage_entry_block_reason(
                        plan=plan,
                        book=retained_book,
                    )
                    if first_stage_block is not None:
                        if active.exit_sold_shares <= 0:
                            self._defer_or_abandon_unfilled_attempt(plan)
                        self.save()
                        return ReversalTickResult(
                            "first_stage_entry_filtered",
                            plan=plan,
                            detail=first_stage_block,
                        )
                if active.execution_phase == "planned":
                    self.strategy.mark_direct_entry_ready(plan)
                    self.save()
            # The health snapshot can be a few hundred milliseconds old by the
            # time the FAK is signed. Refresh immediately before validating the
            # entry and use that exact executable ask for the order.
            if book_refresh is not None:
                up_book, down_book = book_refresh()
            return self._finish_selected_plan(
                market=market,
                up_book=up_book,
                down_book=down_book,
                plan=plan,
            )

        if prepared is None:
            prepared = self.strategy.prepare_opening_split(market.slug)
            if prepared.amount != plan.making_amount:
                raise ReversalRuntimeError(
                    "prepared split amount does not match confirmed trade plan"
                )
            self.save()
        if prepared.execution_phase in {"split_submitting", "split_uncertain"}:
            raise ReversalRuntimeError(
                "opening split outcome is uncertain; automatic retry is blocked pending reconciliation"
            )
        if prepared.execution_phase in {"merge_submitting", "merge_uncertain"}:
            raise ReversalRuntimeError(
                "opening merge outcome is uncertain; automatic retry is blocked pending reconciliation"
            )
        if prepared.execution_phase == "planned":
            self._submit_opening_split(market)

        self.strategy.adopt_opening_split(plan)
        self.save()
        if book_refresh is not None:
            up_book, down_book = book_refresh()
        return self._finish_selected_plan(
            market=market,
            up_book=up_book,
            down_book=down_book,
            plan=plan,
        )

    def _defer_or_abandon_unfilled_attempt(
        self,
        plan: TradePlan,
        *,
        block_trend: bool = True,
    ) -> None:
        if self.strategy.settings.continue_final_stage_until_success_or_unfunded:
            self.strategy.defer_unfilled_attempt(plan)
            return
        self.strategy.abandon_filtered_attempt(plan, block_trend=block_trend)

    def _finish_selected_plan(
        self,
        *,
        market: Market,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
        plan: TradePlan,
    ) -> ReversalTickResult:
        active = self.strategy.state.active_round
        if (
            self.execution_mode == "direct_buy"
            and active is not None
            and active.split_transaction_hash == "direct-buy"
        ):
            if (
                self.strategy.settings.uses_first_stage_order_rules(plan.attempt)
                and active.execution_phase
                in {"direct_entry_ready", "direct_entry_partial"}
            ):
                retained_book = (
                    up_book if plan.retained_side is Direction.UP else down_book
                )
                block_reason = self._first_stage_entry_block_reason(
                    plan=plan,
                    book=retained_book,
                )
                if block_reason is not None:
                    if active.exit_sold_shares <= 0:
                        self._defer_or_abandon_unfilled_attempt(
                            plan, block_trend=False
                        )
                        self.save()
                        return ReversalTickResult(
                            "first_stage_entry_filtered",
                            plan=plan,
                            detail=(
                                f"retry stopped: {block_reason}; "
                                "trend remains eligible next window"
                            ),
                        )
                    return ReversalTickResult(
                        "entry_book_pending",
                        plan=plan,
                        detail=f"partial entry retry waiting: {block_reason}",
                    )
            return self._finish_direct_plan(
                market=market,
                up_book=up_book,
                down_book=down_book,
                plan=plan,
            )
        return self._finish_plan(
            market=market,
            up_book=up_book,
            down_book=down_book,
            plan=plan,
        )

    def _compact_entry_block_reason(
        self,
        *,
        plan: TradePlan,
        book: OrderBookSnapshot,
    ) -> str | None:
        settings = self.strategy.settings
        active = self.strategy.state.active_round
        assert active is not None
        ask = book.quote.ask
        bid = book.quote.bid
        if ask is None or bid is None or ask <= 0 or ask >= 1:
            return "compact order book is incomplete"
        max_ask = (
            settings.compact_first_max_ask
            if plan.attempt == 1
            else settings.compact_second_max_ask
        )
        if ask > max_ask:
            return f"ask {ask} exceeds stage-{plan.attempt} limit {max_ask}"
        if ask - bid > settings.compact_max_spread:
            return f"spread {ask - bid} exceeds {settings.compact_max_spread}"
        remaining = max(Decimal("0"), plan.making_amount - active.exit_sold_shares)
        order_size = (
            _marketable_buy_size(nominal_shares=remaining, price=ask)
            if remaining > 0
            else Decimal("0")
        )
        if order_size > 0 and ask_depth(book, ask) < order_size:
            return "best ask depth cannot fill the compact order"
        remaining_cost = order_size * ask
        remaining_fee = order_size * Decimal("0.07") * ask * (Decimal("1") - ask)
        attempt_cost = active.exit_sell_proceeds + remaining_cost
        attempt_fees = active.entry_fees + remaining_fee
        if attempt_cost > settings.compact_max_order_notional:
            return (
                f"order notional {attempt_cost} exceeds compact limit "
                f"{settings.compact_max_order_notional}"
            )
        projected_loss = active.cumulative_loss + attempt_cost + attempt_fees
        if projected_loss > settings.compact_round_loss_limit:
            return (
                f"projected round loss {projected_loss} exceeds compact limit "
                f"{settings.compact_round_loss_limit}"
            )
        if plan.attempt == 2:
            if self.unlocked_profit_lookup is None:
                return "settled unlocked-profit budget is unavailable"
            unlocked = self.unlocked_profit_lookup(settings.compact_profit_lock_fraction)
            if unlocked < attempt_cost + attempt_fees:
                return (
                    f"unlocked profit {unlocked} cannot fund stage-2 risk "
                    f"{attempt_cost + attempt_fees}"
                )
        return None

    def _round_loss_entry_block_reason(
        self,
        *,
        plan: TradePlan,
        book: OrderBookSnapshot,
    ) -> str | None:
        """Reject an attempt whose complete fill could breach the round hard cap."""
        settings = self.strategy.settings
        hard_limit = settings.hard_round_loss_limit
        if hard_limit is None:
            return None
        active = self.strategy.state.active_round
        assert active is not None
        ask = book.quote.ask
        if ask is None or ask <= 0 or ask >= 1:
            return None
        remaining = max(Decimal("0"), plan.making_amount - active.exit_sold_shares)
        if remaining <= 0:
            return None
        order_size = _marketable_buy_size(nominal_shares=remaining, price=ask)
        remaining_cost = order_size * ask
        remaining_fee = (
            order_size * Decimal("0.07") * ask * (Decimal("1") - ask)
        )
        projected_loss = (
            active.cumulative_loss
            + active.exit_sell_proceeds
            + active.entry_fees
            + remaining_cost
            + remaining_fee
        )
        if projected_loss > hard_limit:
            return (
                f"projected round loss {projected_loss} exceeds hard limit "
                f"{hard_limit}"
            )
        return None

    def _first_stage_entry_block_reason(
        self,
        *,
        plan: TradePlan,
        book: OrderBookSnapshot,
    ) -> str | None:
        """Apply only executable ask, spread, and depth checks to this profile."""
        settings = self.strategy.settings
        active = self.strategy.state.active_round
        assert active is not None
        ask = book.quote.ask
        bid = book.quote.bid
        if ask is None or bid is None or ask <= 0 or ask >= 1:
            return "first-stage order book is incomplete"
        if ask > settings.first_stage_only_max_ask:
            return (
                f"ask {ask} exceeds first-stage limit "
                f"{settings.first_stage_only_max_ask}"
            )
        spread = ask - bid
        if spread > settings.first_stage_only_max_spread:
            return (
                f"spread {spread} exceeds "
                f"{settings.first_stage_only_max_spread}"
            )
        remaining = max(Decimal("0"), plan.making_amount - active.exit_sold_shares)
        order_size = (
            _marketable_buy_size(nominal_shares=remaining, price=ask)
            if remaining > 0
            else Decimal("0")
        )
        if order_size > 0 and ask_depth(book, ask) < order_size:
            return "best ask depth cannot fill the first-stage order"
        return None

    def _finish_direct_plan(
        self,
        *,
        market: Market,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
        plan: TradePlan,
    ) -> ReversalTickResult:
        active = self.strategy.state.active_round
        assert active is not None
        if not self.live:
            if active.execution_phase != "direct_entry_complete":
                remaining = max(Decimal("0"), plan.making_amount - active.exit_sold_shares)
                book = up_book if plan.retained_side is Direction.UP else down_book
                ask = book.quote.ask or Decimal("0.50")
                self.strategy.record_direct_entry_fill(
                    plan, shares=remaining, cost=remaining * ask
                )
                self.save()
            return ReversalTickResult("paper_complete", plan=plan)
        if self.trader is None:
            raise ReversalRuntimeError("live reversal runtime requires trader")
        if active.execution_phase == "direct_entry_complete":
            return ReversalTickResult("awaiting_settlement", plan=plan)

        retained_index = 0 if plan.retained_side is Direction.UP else 1
        token_id = market.token_ids[retained_index]
        book = up_book if retained_index == 0 else down_book
        remaining = max(Decimal("0"), plan.making_amount - active.exit_sold_shares)

        # A restart after submission must reconcile the unique market-token
        # balance before another order can be sent, preventing a duplicate buy.
        if active.execution_phase == "direct_entry_submitting":
            available = self.trader.conditional_balance(token_id, self.signature_type)
            newly_visible = max(Decimal("0"), available - active.exit_sold_shares)
            if newly_visible > Decimal("0.000001"):
                shares = min(remaining, newly_visible)
                price = active.entry_submitted_price or book.quote.ask or Decimal("1")
                complete = self.strategy.record_direct_entry_fill(
                    plan, shares=shares, cost=shares * price
                )
                self.save()
                return ReversalTickResult(
                    "entry_reconciled" if complete else "entry_partial",
                    plan=plan,
                )
            self.strategy.mark_direct_entry_retryable(plan)
            self.save()

        ask = book.quote.ask
        if ask is None or ask <= 0 or ask >= 1:
            return ReversalTickResult("entry_book_pending", plan=plan)
        order_size = _marketable_buy_size(nominal_shares=remaining, price=ask)
        self.strategy.mark_direct_entry_submitting(plan, ask)
        self.save()
        try:
            response = self.trader.buy_limit(
                token_id,
                ask,
                order_size,
                market.minimum_tick_size,
                market.neg_risk,
                "FAK",
            )
        except Exception as exc:
            amount_rejected = _is_buy_amount_precision_error(exc)
            if not _is_fak_no_match_error(exc) and not amount_rejected:
                raise
            logger.warning(
                "REVERSAL_ENTRY_RETRYABLE slug=%s side=%s price=%s size=%s reason=%s",
                market.slug,
                plan.retained_side.value,
                ask,
                order_size,
                "amount_precision_rejected" if amount_rejected else "fak_unmatched",
            )
            self.strategy.record_execution(
                unmatched_orders=1,
                api_order_errors=1 if amount_rejected else 0,
            )
            self.strategy.mark_direct_entry_retryable(plan)
            first_stage_retry_attempt = 0
            first_stage_retry_scheduled = False
            first_stage_retry_exhausted = False
            if (
                self.strategy.settings.uses_first_stage_order_rules(plan.attempt)
                and not amount_rejected
                and active.exit_sold_shares <= 0
            ):
                first_stage_retry_attempt = (
                    self.strategy.record_direct_entry_unmatched(plan)
                )
                first_stage_retry_exhausted = (
                    first_stage_retry_attempt
                    >= self.strategy.settings.first_stage_only_max_fak_attempts
                )
                if first_stage_retry_exhausted:
                    # A zero-fill FAK must not consume the whole trend. Stop
                    # retrying this window, then permit a fresh attempt in the
                    # next window if the same streak is still intact.
                    self._defer_or_abandon_unfilled_attempt(
                        plan, block_trend=False
                    )
                else:
                    first_stage_retry_scheduled = True
            elif (
                (
                    self.strategy.settings.compact_two_stage_enabled
                    or self.strategy.settings.uses_first_stage_order_rules(plan.attempt)
                    or bool(self.strategy.settings.fixed_notional_stages)
                )
                and active.exit_sold_shares <= 0
            ):
                self._defer_or_abandon_unfilled_attempt(plan)
            self.save()
            return ReversalTickResult(
                (
                    "entry_unmatched"
                    if first_stage_retry_scheduled
                    else "compact_fak_skipped"
                    if self.strategy.settings.compact_two_stage_enabled
                    else "first_stage_fak_skipped"
                    if self.strategy.settings.uses_first_stage_order_rules(plan.attempt)
                    else "fixed_notional_fak_skipped"
                    if self.strategy.settings.fixed_notional_stages
                    else "entry_amount_rejected" if amount_rejected else "entry_unmatched"
                ),
                plan=plan,
                detail=(
                    (
                        f"FAK zero fill; retry {first_stage_retry_attempt + 1}/"
                        f"{self.strategy.settings.first_stage_only_max_fak_attempts} "
                        "scheduled on the next fresh book"
                    )
                    if first_stage_retry_scheduled
                    else (
                        f"FAK zero fill after {first_stage_retry_attempt} attempts; "
                        "this window ended without locking the trend"
                    )
                    if first_stage_retry_exhausted
                    else "bounded attempt ended after a conclusive CLOB rejection"
                    if (
                        self.strategy.settings.compact_two_stage_enabled
                        or self.strategy.settings.uses_first_stage_order_rules(plan.attempt)
                        or bool(self.strategy.settings.fixed_notional_stages)
                    )
                    else "CLOB conclusively rejected BUY amount precision; live-book resize retry scheduled"
                    if amount_rejected
                    else "FAK had no immediately matchable counterparty; retry scheduled"
                ),
            )
        cost, shares = _buy_fill_amounts(response)
        order = {
            "slug": market.slug,
            "side": plan.retained_side.value,
            "token_id": token_id,
            "price": str(ask),
            "size": str(order_size),
            "order_type": "FAK",
            "order_role": "reversal_direct_entry",
            "round_id": plan.round_id,
            "attempt": plan.attempt,
            "trend_side": plan.trend_side.value,
            "planned_shares": str(plan.making_amount),
            "soft_limit_final_recovery": active.soft_limit_final_recovery,
            "response": response,
        }
        if shares > 0:
            complete = self.strategy.record_direct_entry_fill(
                plan, shares=shares, cost=cost
            )
            order["entry_complete"] = complete
            self.save()
            if self.order_callback is not None:
                self.order_callback(order)
            return ReversalTickResult(
                "entry_complete" if complete else "entry_partial",
                plan=plan,
                order=order,
            )
        self.strategy.record_execution(unmatched_orders=1)
        self.strategy.mark_direct_entry_retryable(plan)
        if (
            self.strategy.settings.uses_first_stage_order_rules(plan.attempt)
            and active.exit_sold_shares <= 0
        ):
            retry_attempt = self.strategy.record_direct_entry_unmatched(plan)
            if retry_attempt >= self.strategy.settings.first_stage_only_max_fak_attempts:
                self._defer_or_abandon_unfilled_attempt(plan, block_trend=False)
                self.save()
                return ReversalTickResult(
                    "first_stage_fak_skipped",
                    plan=plan,
                    order=order,
                    detail=(
                        f"FAK zero fill after {retry_attempt} attempts; "
                        "this window ended without locking the trend"
                    ),
                )
            self.save()
            return ReversalTickResult(
                "entry_unmatched",
                plan=plan,
                order=order,
                detail=(
                    f"FAK zero fill; retry {retry_attempt + 1}/"
                    f"{self.strategy.settings.first_stage_only_max_fak_attempts} "
                    "scheduled on the next fresh book"
                ),
            )
        self.save()
        return ReversalTickResult(
            "entry_unmatched",
            plan=plan,
            order=order,
        )

    def prepare_active_round_opening_split(
        self,
        market: Market,
    ) -> ReversalTickResult | None:
        """Pre-split the next stake while the prior result is still pending."""
        active = self.strategy.state.active_round
        prepared = self.strategy.state.prepared_split
        if prepared is not None:
            if prepared.window_slug != market.slug:
                raise ReversalRuntimeError(
                    f"prepared split for {prepared.window_slug} must be reconciled first"
                )
            if prepared.execution_phase == "split_confirmed":
                return ReversalTickResult(
                    "opening_split_ready",
                    detail=f"amount={prepared.amount} tx={prepared.transaction_hash}",
                )
            raise ReversalRuntimeError(
                "opening split outcome is uncertain; automatic retry is blocked pending reconciliation"
            )
        if (
            active is None
            or active.awaiting_window is None
            or active.execution_phase != "trend_exit_complete"
            or previous_5m_slug(market.slug) != active.awaiting_window
            or active.failures + 1 >= self.strategy.settings.attempt_limit
        ):
            return None

        prepared = self.strategy.prepare_opening_split(market.slug)
        self.save()
        self._submit_opening_split(market)
        prepared = self.strategy.state.prepared_split
        assert prepared is not None
        return ReversalTickResult(
            "opening_split_prepared",
            detail=f"amount={prepared.amount} tx={prepared.transaction_hash}",
        )

    def _submit_opening_split(self, market: Market) -> None:
        prepared = self.strategy.state.prepared_split
        if prepared is None or prepared.execution_phase != "planned":
            raise ReversalRuntimeError("opening split is not ready for submission")
        self.strategy.mark_opening_split_submitting()
        self.save()
        if self.live:
            if self.splitter is None or self.trader is None:
                raise ReversalRuntimeError(
                    "live reversal runtime requires splitter and trader"
                )
            try:
                receipt = self.splitter.split(
                    condition_id=market.condition_id,
                    up_token_id=market.token_ids[0],
                    down_token_id=market.token_ids[1],
                    amount=prepared.amount,
                    neg_risk=market.neg_risk,
                )
            except Exception:
                self.strategy.mark_opening_split_uncertain()
                self.save()
                raise
            transaction_hash = str(receipt.transaction_hash)
        else:
            transaction_hash = "paper-opening-split"
        self.strategy.mark_opening_split_confirmed(transaction_hash)
        self.save()

    def _merge_unused_opening_split(
        self,
        market: Market,
        amount: Decimal,
    ) -> ReversalTickResult:
        if not self.live:
            self.strategy.mark_opening_merge_submitting()
            self.strategy.mark_opening_merge_confirmed()
            self.save()
            return ReversalTickResult("paper_unused_split_merged")
        if self.splitter is None:
            raise ReversalRuntimeError("live reversal merge requires splitter")
        self.strategy.mark_opening_merge_submitting()
        self.save()
        try:
            receipt = self.splitter.merge(
                condition_id=market.condition_id,
                up_token_id=market.token_ids[0],
                down_token_id=market.token_ids[1],
                amount=amount,
                neg_risk=market.neg_risk,
            )
        except Exception:
            self.strategy.mark_opening_merge_uncertain()
            self.save()
            raise
        self.strategy.mark_opening_merge_confirmed()
        self.save()
        return ReversalTickResult(
            "unused_split_merged",
            detail=f"tx={receipt.transaction_hash}",
        )

    def _finish_plan(
        self,
        *,
        market: Market,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
        plan: TradePlan,
    ) -> ReversalTickResult:
        active = self.strategy.state.active_round
        assert active is not None
        if not self.live:
            if active.execution_phase != "trend_exit_complete":
                remaining = max(
                    Decimal("0"), plan.making_amount - active.exit_sold_shares
                )
                self.strategy.record_exit_fill(
                    plan,
                    shares=remaining,
                    proceeds=remaining * Decimal("0.50"),
                )
                self.save()
            return ReversalTickResult("paper_complete", plan=plan)

        if self.trader is None:
            raise ReversalRuntimeError("live reversal runtime requires trader")

        active = self.strategy.state.active_round
        assert active is not None
        if active.execution_phase == "trend_exit_complete":
            return ReversalTickResult("awaiting_settlement", plan=plan)

        trend_index = 0 if plan.trend_side is Direction.UP else 1
        trend_token = market.token_ids[trend_index]
        trend_book = up_book if trend_index == 0 else down_book
        available = self.trader.conditional_balance(trend_token, self.signature_type)
        expected_remaining = max(
            Decimal("0"), plan.making_amount - active.exit_sold_shares
        )
        if (
            available <= Decimal("0.000001")
            and active.execution_phase == "trend_exit_submitting"
        ):
            self.strategy.reconcile_exit_complete(plan)
            self.save()
            return ReversalTickResult("exit_reconciled", plan=plan)
        sell_size = min(expected_remaining, available)
        if sell_size <= 0:
            return ReversalTickResult(
                "exit_balance_pending", plan=plan, detail=f"available={available}"
            )
        best_bid = trend_book.quote.bid
        if best_bid is None or best_bid <= 0:
            return ReversalTickResult("exit_book_pending", plan=plan)

        prices = [best_bid]
        latest_order: dict[str, Any] | None = None
        for price in prices:
            if active.execution_phase == "trend_exit_submitting":
                self.strategy.mark_exit_retryable(plan)
            self.strategy.mark_exit_submitting(plan)
            self.save()
            try:
                response = self.trader.sell_limit(
                    trend_token,
                    price,
                    sell_size,
                    market.minimum_tick_size,
                    market.neg_risk,
                    "FAK",
                )
            except Exception as exc:
                if not _is_fak_no_match_error(exc):
                    raise
                logger.warning(
                    "REVERSAL_EXIT_FAK_UNMATCHED slug=%s side=%s price=%s "
                    "size=%s; retrying against the next live book",
                    market.slug,
                    plan.trend_side.value,
                    price,
                    sell_size,
                )
                self.strategy.mark_exit_retryable(plan)
                self.strategy.record_execution(unmatched_orders=1)
                self.save()
                return ReversalTickResult(
                    "exit_unmatched",
                    plan=plan,
                    detail="FAK had no immediately matchable counterparty; retry scheduled",
                )
            latest_order = {
                "slug": market.slug,
                "side": plan.trend_side.value,
                "token_id": trend_token,
                "price": str(price),
                "size": str(sell_size),
                "order_type": "FAK",
                "order_role": "reversal_trend_exit",
                "round_id": plan.round_id,
                "attempt": plan.attempt,
                "retained_side": plan.retained_side.value,
                "split_amount": str(plan.making_amount),
                "split_transaction_hash": active.split_transaction_hash,
                "response": response,
            }
            shares, proceeds = _sell_fill_amounts(response)
            if shares > 0:
                complete = self.strategy.record_exit_fill(
                    plan, shares=shares, proceeds=proceeds
                )
                refreshed_active = self.strategy.state.active_round
                assert refreshed_active is not None
                latest_order["exit_complete"] = complete
                latest_order["cumulative_exit_proceeds"] = str(
                    refreshed_active.exit_sell_proceeds
                )
                self.save()
                if self.order_callback is not None:
                    self.order_callback(latest_order)
                return ReversalTickResult(
                    "exit_complete" if complete else "exit_partial",
                    plan=plan,
                    order=latest_order,
                )
            self.strategy.mark_exit_retryable(plan)
            self.save()
        self.strategy.record_execution(unmatched_orders=1)
        self.save()
        return ReversalTickResult("exit_unmatched", plan=plan, order=latest_order)

    def _immediate_results_available(self, current_slug: str) -> bool:
        count = self.strategy.settings.trigger_streak
        expected = [
            previous_5m_slug(current_slug, offset)
            for offset in range(count, 0, -1)
        ]
        return self.strategy.state.recent_slugs[-count:] == expected

    def _confirmed_trend_side(self) -> Direction | None:
        active = self.strategy.state.active_round
        if active is not None:
            return active.trend_side
        count = self.strategy.settings.trigger_streak
        results = self.strategy.state.recent_results
        if len(results) >= count and len(set(results[-count:])) == 1:
            return results[-1]
        return None


def _sell_fill_amounts(response: dict[str, Any]) -> tuple[Decimal, Decimal]:
    if not isinstance(response, dict) or str(response.get("status", "")).lower() != "matched":
        return Decimal("0"), Decimal("0")
    try:
        shares = Decimal(str(response.get("makingAmount") or "0"))
        proceeds = Decimal(str(response.get("takingAmount") or "0"))
    except (ArithmeticError, ValueError):
        return Decimal("0"), Decimal("0")
    if shares <= 0 or proceeds <= 0:
        return Decimal("0"), Decimal("0")
    return shares, proceeds


def _buy_fill_amounts(response: dict[str, Any]) -> tuple[Decimal, Decimal]:
    if not isinstance(response, dict) or str(response.get("status", "")).lower() != "matched":
        return Decimal("0"), Decimal("0")
    try:
        cost = Decimal(str(response.get("makingAmount") or "0"))
        shares = Decimal(str(response.get("takingAmount") or "0"))
    except (ArithmeticError, ValueError):
        return Decimal("0"), Decimal("0")
    if cost <= 0 or shares <= 0:
        return Decimal("0"), Decimal("0")
    return cost, shares


def _marketable_buy_size(
    *,
    nominal_shares: Decimal,
    price: Decimal,
) -> Decimal:
    """Return the smallest exchange-valid size for an immediately marketable BUY.

    Polymarket validates a marketable BUY with maker (cash) precision of two
    decimals and taker (shares) precision of at most four decimals. We use a
    stricter two-decimal share grid so both constraints can be satisfied exactly.
    """
    if nominal_shares <= 0 or price <= 0:
        raise ValueError("marketable BUY size and price must be positive")
    size = max(
        nominal_shares,
        (MIN_MARKETABLE_BUY_NOTIONAL / price).quantize(
            ORDER_SIZE_QUANTUM,
            rounding=ROUND_UP,
        ),
    ).quantize(ORDER_SIZE_QUANTUM, rounding=ROUND_UP)
    for _ in range(10_000):
        maker_amount = size * price
        if (
            maker_amount >= MIN_MARKETABLE_BUY_NOTIONAL
            and maker_amount == maker_amount.quantize(MAKER_AMOUNT_QUANTUM)
        ):
            return size
        size += ORDER_SIZE_QUANTUM
    raise ReversalRuntimeError(
        f"could not find exchange-valid marketable BUY size for price={price}"
    )


def _affordable_marketable_buy_size(
    *,
    available_collateral: Decimal,
    price: Decimal,
) -> Decimal:
    """Return the largest exchange-valid BUY size that does not exceed cash."""
    if available_collateral <= 0 or price <= 0:
        return Decimal("0")
    cash_budget = available_collateral.quantize(
        MAKER_AMOUNT_QUANTUM,
        rounding=ROUND_DOWN,
    )
    if cash_budget < MIN_MARKETABLE_BUY_NOTIONAL:
        return Decimal("0")
    size = (cash_budget / price).quantize(
        ORDER_SIZE_QUANTUM,
        rounding=ROUND_DOWN,
    )
    for _ in range(10_000):
        if size <= 0:
            return Decimal("0")
        maker_amount = size * price
        if (
            MIN_MARKETABLE_BUY_NOTIONAL <= maker_amount <= cash_budget
            and maker_amount == maker_amount.quantize(MAKER_AMOUNT_QUANTUM)
        ):
            return size
        size -= ORDER_SIZE_QUANTUM
    raise ReversalRuntimeError(
        f"could not find an affordable marketable BUY size for price={price}"
    )


def _is_fak_no_match_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "no orders found to match with fak order" in message
        or "fak orders are partially filled or killed if no match is found" in message
    )


def _is_buy_amount_precision_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "invalid amounts" in message
        and "market buy orders maker amount supports a max accuracy of 2 decimals"
        in message
        and "taker amount a max of 4 decimals" in message
    )
