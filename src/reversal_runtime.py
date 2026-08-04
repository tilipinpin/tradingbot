from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Callable, Protocol

from src.polymarket import Market, OrderBookSnapshot
from src.polygon_split import CompleteSetSplitter
from src.reversal_v11 import (
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


logger = logging.getLogger(__name__)
MIN_MARKETABLE_BUY_NOTIONAL = Decimal("1.01")
ORDER_SIZE_QUANTUM = Decimal("0.01")
MAKER_AMOUNT_QUANTUM = Decimal("0.01")


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
        if collateral < required_collateral:
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
        self._next_gamma_verification_at = 0.0

    def save(self) -> None:
        self.strategy.dump(self.state_path)

    def send_daily_report_once(
        self,
        report_day: date,
        sender: Callable[[str], bool],
    ) -> bool:
        key = report_day.isoformat()
        if key in self.strategy.state.reported_days:
            return False
        if not sender(format_daily_report(report_day, self.strategy.metrics(report_day))):
            return False
        self.strategy.state.reported_days = (
            self.strategy.state.reported_days + [key]
        )[-14:]
        self.save()
        return True

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
            result = Direction.UP if closing_price >= opening_price else Direction.DOWN
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

    def observe_completed_window_prices(
        self,
        completed_prices: dict[str, tuple[Decimal, Decimal]],
    ) -> list[tuple[str, str]]:
        """Settle windows from completed Polymarket openPrice/closePrice pairs."""
        observed: list[tuple[str, str]] = []
        last_epoch = (
            slug_epoch(self.strategy.state.last_settled_slug)
            if self.strategy.state.last_settled_slug
            else None
        )
        for slug in sorted(completed_prices, key=slug_epoch):
            epoch = slug_epoch(slug)
            if last_epoch is not None and epoch <= last_epoch:
                continue
            opening_price, closing_price = completed_prices[slug]
            if opening_price <= 0 or closing_price <= 0:
                raise ReversalRuntimeError(
                    f"invalid completed price for {slug}: {opening_price} -> {closing_price}"
                )
            result = (
                Direction.UP if closing_price >= opening_price else Direction.DOWN
            )
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
        if verified:
            self.save()
        return verified

    def quarantine_gamma_mismatch(self, mismatch: GammaResultMismatch) -> None:
        """Persist a conflict once so it cannot pause every following window."""
        self.strategy.quarantine_gamma_mismatch(mismatch.slug)
        self.save()

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
                if (
                    (
                        self.strategy.settings.first_stage_rv60_filter_enabled
                        and selected_health.short_volatility
                        >= self.strategy.settings.first_stage_max_rv60
                    )
                    or (
                        self.strategy.settings.first_stage_rv300_filter_enabled
                        and selected_health.five_minute_volatility
                        >= self.strategy.settings.first_stage_max_rv300
                    )
                ):
                    return ReversalTickResult(
                        "first_stage_extreme_volatility_no_split",
                        detail=(
                            f"RV60={selected_health.short_volatility} >= "
                            f"{self.strategy.settings.first_stage_max_rv60}; "
                            f"RV300={selected_health.five_minute_volatility} >= "
                            f"{self.strategy.settings.first_stage_max_rv300}"
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
            if active.execution_phase in {
                "planned",
                "direct_entry_ready",
                "direct_entry_partial",
            }:
                if (
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
                        minimum_shares=(
                            self.strategy.settings.stakes[plan.attempt - 1]
                            if plan.attempt <= 4
                            else Decimal("0")
                        ),
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
                        if self.strategy.settings.minimum_round_profit > 0:
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
                                    "profit_target_unfunded",
                                    plan=plan,
                                    detail=(
                                        f"required {desired_additional * ask} pUSD to cover "
                                        f"round loss plus "
                                        f"{self.strategy.settings.minimum_round_profit} "
                                        f"pUSD profit; available {available_collateral}"
                                    ),
                                )
                            return ReversalTickResult(
                                "entry_balance_insufficient",
                                plan=plan,
                                detail=(
                                    f"partial entry requires {desired_additional * ask} "
                                    f"pUSD more to preserve the "
                                    f"{self.strategy.settings.minimum_round_profit} "
                                    f"pUSD round-profit target; available "
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
                if active.execution_phase == "planned":
                    self.strategy.mark_direct_entry_ready(plan)
                    self.save()
            return self._finish_direct_plan(
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
            self.save()
            return ReversalTickResult(
                "entry_amount_rejected" if amount_rejected else "entry_unmatched",
                plan=plan,
                detail=(
                    "CLOB conclusively rejected BUY amount precision; live-book resize retry scheduled"
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
            or active.failures + 1 >= len(self.strategy.settings.stakes)
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
