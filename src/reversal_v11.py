from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from src.polygon_split import CompleteSetSplitter, SplitReceipt


MAX_REVERSAL_STAGES = 25
COMPACT_REVERSAL_STAKES = (Decimal("2"), Decimal("4"))
FIRST_STAGE_ONLY_STAKES = (Decimal("1"),)
TWO_WINDOW_FIXED_NOTIONALS = (
    Decimal("1"),
    Decimal("2"),
    Decimal("2"),
    Decimal("2"),
    Decimal("2"),
    Decimal("1"),
    Decimal("2"),
    Decimal("4"),
    Decimal("8"),
    Decimal("16"),
    Decimal("32"),
    Decimal("64"),
)
SPARSE_RECOVERY_NOTIONALS = (
    Decimal("4"),
    Decimal("0"),
    Decimal("0"),
    Decimal("1"),
    Decimal("2"),
    Decimal("4"),
    Decimal("8"),
    Decimal("16"),
    Decimal("32"),
    Decimal("64"),
)
REVERSAL_STAGE_STAKES = (
    Decimal("1"),
    Decimal("2"),
    Decimal("4"),
    Decimal("8"),
    Decimal("16"),
) + (Decimal("16"),) * (MAX_REVERSAL_STAGES - 5)


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"

    @property
    def opposite(self) -> "Direction":
        return Direction.DOWN if self is Direction.UP else Direction.UP


@dataclass(frozen=True)
class ReversalSettings:
    trigger_streak: int = 2
    stakes: tuple[Decimal, ...] = REVERSAL_STAGE_STAKES
    maximum_attempts: int | None = None
    preferred_sell_price: Decimal = Decimal("0.50")
    max_short_volatility: Decimal = Decimal("0.0015")
    max_window_move: Decimal = Decimal("0.0030")
    min_exit_bid_depth: Decimal = Decimal("2")
    max_spread: Decimal = Decimal("0.05")
    market_filters_enabled: bool = False
    first_stage_rv60_filter_enabled: bool = False
    first_stage_max_rv60: Decimal = Decimal("0.0010")
    first_stage_rv300_filter_enabled: bool = False
    first_stage_max_rv300: Decimal = Decimal("0.0020")
    first_stage_rv300_persistence_ratio: Decimal = Decimal("0.35")
    first_stage_rv300_hard_multiplier: Decimal = Decimal("2.0")
    dynamic_final_recovery_enabled: bool = False
    dynamic_recovery_start_attempt: int = 5
    full_loss_recovery_enabled: bool = True
    full_loss_recovery_start_attempt: int = 6
    full_loss_recovery_strict_funding: bool = False
    continue_final_stage_until_success_or_unfunded: bool = False
    minimum_round_profit: Decimal = Decimal("0")
    late_stage_recovery_fraction: Decimal = Decimal("1.00")
    recovery_fraction: Decimal = Decimal("0.50")
    recovery_min_expected_value: Decimal = Decimal("0.05")
    recovery_min_open_cross_usd: Decimal = Decimal("2")
    recovery_min_entry_price: Decimal = Decimal("0.46")
    recovery_max_entry_price: Decimal = Decimal("0.62")
    recovery_max_shares: Decimal = Decimal("16")
    allocated_capital: Decimal = Decimal("110")
    maximum_streak_loss: Decimal = Decimal("110")
    hard_round_loss_limit: Decimal | None = None
    soft_round_loss_limit: Decimal | None = None
    one_final_recovery_after_soft_limit: bool = False
    end_round_at_soft_loss_limit: bool = False
    compact_two_stage_enabled: bool = False
    compact_first_max_ask: Decimal = Decimal("0.50")
    compact_second_max_ask: Decimal = Decimal("0.45")
    compact_max_spread: Decimal = Decimal("0.03")
    compact_round_loss_limit: Decimal = Decimal("3")
    compact_max_order_notional: Decimal = Decimal("1.90")
    compact_profit_lock_fraction: Decimal = Decimal("0.70")
    compact_loss_round_limit: int = 5
    compact_pause_windows: int = 3
    first_stage_only_enabled: bool = False
    first_attempt_uses_first_stage_rules: bool = False
    first_stage_only_max_ask: Decimal = Decimal("0.64")
    first_stage_only_max_spread: Decimal = Decimal("0.05")
    first_stage_only_max_fak_attempts: int = 3
    fixed_notional_stages: tuple[Decimal, ...] = ()
    fixed_notional_max_ask: Decimal = Decimal("0.49")
    fixed_notional_recovery_start_attempt: int | None = None
    fixed_notional_recovery_loss_start_attempt: int | None = None
    sparse_recovery_notional_stages: tuple[Decimal, ...] = ()
    sparse_recovery_start_attempt: int = 5
    sparse_recovery_loss_start_attempt: int = 4

    def __post_init__(self) -> None:
        if self.trigger_streak < 2:
            raise ValueError("trigger streak must be at least two windows")
        if self.maximum_attempts is not None and not 1 <= self.maximum_attempts <= len(
            self.stakes
        ):
            raise ValueError("maximum attempts must fit within the stage sequence")
        selected_bounded_profiles = sum(
            (
                self.compact_two_stage_enabled,
                self.first_stage_only_enabled,
                bool(self.fixed_notional_stages),
                bool(self.sparse_recovery_notional_stages),
            )
        )
        if selected_bounded_profiles > 1:
            raise ValueError("reversal profiles cannot be combined")
        expected_stakes = (
            self.sparse_recovery_notional_stages
            if self.sparse_recovery_notional_stages
            else self.fixed_notional_stages
            if self.fixed_notional_stages
            else FIRST_STAGE_ONLY_STAKES
            if self.first_stage_only_enabled
            else COMPACT_REVERSAL_STAKES
            if self.compact_two_stage_enabled
            else REVERSAL_STAGE_STAKES
        )
        if self.stakes != expected_stakes:
            raise ValueError(
                "invalid stage sequence for the selected reversal profile"
            )
        if not self.sparse_recovery_notional_stages and any(
            value <= 0 for value in self.stakes
        ):
            raise ValueError("stakes must be positive")
        if self.sparse_recovery_notional_stages and (
            any(value < 0 for value in self.sparse_recovery_notional_stages)
            or not any(value > 0 for value in self.sparse_recovery_notional_stages)
            or not 1
            <= self.sparse_recovery_start_attempt
            <= len(self.sparse_recovery_notional_stages)
            or not 1
            <= self.sparse_recovery_loss_start_attempt
            < self.sparse_recovery_start_attempt
        ):
            raise ValueError("invalid sparse-recovery reversal settings")
        if self.dynamic_recovery_start_attempt != 5:
            raise ValueError("dynamic recovery must start on attempt 5")
        if self.first_stage_max_rv60 <= 0:
            raise ValueError("first-stage RV60 threshold must be positive")
        if self.first_stage_max_rv300 <= 0:
            raise ValueError("first-stage RV300 threshold must be positive")
        if not Decimal("0") < self.first_stage_rv300_persistence_ratio <= Decimal("1"):
            raise ValueError("first-stage RV300 persistence ratio must be in (0, 1]")
        if self.first_stage_rv300_hard_multiplier < Decimal("1"):
            raise ValueError("first-stage RV300 hard multiplier must be at least one")
        if (
            self.full_loss_recovery_enabled
            and not self.compact_two_stage_enabled
            and not self.first_stage_only_enabled
            and not 2 <= self.full_loss_recovery_start_attempt <= len(self.stakes)
        ):
            raise ValueError(
                "full-loss recovery must start between attempt 2 and the final stage"
            )
        if self.minimum_round_profit < 0:
            raise ValueError("minimum round profit must not be negative")
        if not Decimal("0") < self.late_stage_recovery_fraction <= Decimal("1"):
            raise ValueError("late_stage_recovery_fraction must be in (0, 1]")
        if not Decimal("0") < self.preferred_sell_price < Decimal("1"):
            raise ValueError("preferred sell price must be between zero and one")
        if not Decimal("0") < self.recovery_fraction <= Decimal("1"):
            raise ValueError("recovery_fraction must be in (0, 1]")
        if self.recovery_min_expected_value < 0:
            raise ValueError("recovery_min_expected_value must not be negative")
        if self.recovery_min_open_cross_usd <= 0:
            raise ValueError("recovery_min_open_cross_usd must be positive")
        if not (
            Decimal("0") < self.recovery_min_entry_price
            <= self.recovery_max_entry_price < Decimal("1")
        ):
            raise ValueError("invalid dynamic recovery entry-price range")
        if (
            self.recovery_max_shares <= 0
            or self.allocated_capital <= 0
            or self.maximum_streak_loss <= 0
        ):
            raise ValueError("dynamic recovery risk limits must be positive")
        if self.hard_round_loss_limit is not None and self.hard_round_loss_limit <= 0:
            raise ValueError("hard round loss limit must be positive when configured")
        if self.soft_round_loss_limit is not None and self.soft_round_loss_limit <= 0:
            raise ValueError("soft round loss limit must be positive when configured")
        if self.one_final_recovery_after_soft_limit and self.soft_round_loss_limit is None:
            raise ValueError("final soft-limit recovery requires a soft loss limit")
        if self.end_round_at_soft_loss_limit and self.soft_round_loss_limit is None:
            raise ValueError("soft-limit round exit requires a soft loss limit")
        if not Decimal("0") < self.compact_profit_lock_fraction < Decimal("1"):
            raise ValueError("compact profit lock fraction must be between zero and one")
        if (
            self.compact_first_max_ask <= 0
            or self.compact_second_max_ask <= 0
            or self.compact_first_max_ask >= 1
            or self.compact_second_max_ask >= 1
            or self.compact_max_spread < 0
            or self.compact_round_loss_limit <= 0
            or self.compact_max_order_notional <= 0
            or self.compact_loss_round_limit < 1
            or self.compact_pause_windows < 0
        ):
            raise ValueError("invalid compact reversal risk settings")
        if (
            self.first_stage_only_max_ask <= 0
            or self.first_stage_only_max_ask >= 1
            or self.first_stage_only_max_spread < 0
            or self.first_stage_only_max_fak_attempts < 1
        ):
            raise ValueError("invalid first-stage-only order-book settings")
        if self.fixed_notional_stages and (
            any(value <= 0 for value in self.fixed_notional_stages)
            or self.fixed_notional_max_ask <= 0
            or self.fixed_notional_max_ask >= 1
        ):
            raise ValueError("invalid fixed-notional reversal settings")
        fixed_recovery_bounds = (
            self.fixed_notional_recovery_start_attempt,
            self.fixed_notional_recovery_loss_start_attempt,
        )
        if any(value is not None for value in fixed_recovery_bounds):
            if (
                not self.fixed_notional_stages
                or any(value is None for value in fixed_recovery_bounds)
                or not 1
                <= self.fixed_notional_recovery_loss_start_attempt
                < self.fixed_notional_recovery_start_attempt
                <= len(self.fixed_notional_stages)
            ):
                raise ValueError("invalid fixed-notional recovery settings")

    def uses_dynamic_recovery(self, attempt: int) -> bool:
        return (
            self.dynamic_final_recovery_enabled
            and self.dynamic_recovery_start_attempt <= attempt <= self.attempt_limit
        )

    @property
    def attempt_limit(self) -> int:
        return self.maximum_attempts or len(self.stakes)

    def uses_full_loss_recovery(self, attempt: int) -> bool:
        return (
            not self.compact_two_stage_enabled
            and not self.first_stage_only_enabled
            and not self.fixed_notional_stages
            and not self.sparse_recovery_notional_stages
            and self.full_loss_recovery_enabled
            and self.full_loss_recovery_start_attempt <= attempt <= self.attempt_limit
        )

    def uses_sparse_full_loss_recovery(self, attempt: int) -> bool:
        return bool(self.sparse_recovery_notional_stages) and (
            self.sparse_recovery_start_attempt <= attempt <= self.attempt_limit
        )

    def uses_fixed_notional_full_loss_recovery(self, attempt: int) -> bool:
        return (
            bool(self.fixed_notional_stages)
            and self.fixed_notional_recovery_start_attempt is not None
            and self.fixed_notional_recovery_start_attempt
            <= attempt
            <= self.attempt_limit
        )

    def tracks_direct_loss(self, attempt: int) -> bool:
        """Return whether a failed direct-buy stage belongs to its recovery segment."""
        if self.sparse_recovery_notional_stages:
            return attempt >= self.sparse_recovery_loss_start_attempt
        if self.fixed_notional_recovery_loss_start_attempt is not None:
            return attempt >= self.fixed_notional_recovery_loss_start_attempt
        return True

    def uses_first_stage_order_rules(self, attempt: int) -> bool:
        return self.first_stage_only_enabled or (
            self.first_attempt_uses_first_stage_rules and attempt == 1
        )

    def first_stage_volatility_block_reason(
        self,
        short_volatility: Decimal,
        five_minute_volatility: Decimal,
    ) -> str | None:
        """Block current extremes without treating decayed RV300 as current risk."""
        if (
            self.first_stage_rv60_filter_enabled
            and short_volatility >= self.first_stage_max_rv60
        ):
            return "rv60_extreme"
        if (
            not self.first_stage_rv300_filter_enabled
            or five_minute_volatility < self.first_stage_max_rv300
        ):
            return None
        if (
            five_minute_volatility
            >= self.first_stage_max_rv300
            * self.first_stage_rv300_hard_multiplier
        ):
            return "rv300_hard_extreme"
        if (
            short_volatility
            >= five_minute_volatility
            * self.first_stage_rv300_persistence_ratio
        ):
            return "rv300_persistent"
        return None


@dataclass(frozen=True)
class MarketHealth:
    short_volatility: Decimal
    absolute_window_move: Decimal
    trend_bid_depth: Decimal
    trend_spread: Decimal
    estimated_sellable: bool
    market_data_ok: bool = True
    trading_api_ok: bool = True
    five_minute_volatility: Decimal = Decimal("0")

    def blocked_reasons(self, settings: ReversalSettings) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.market_data_ok:
            reasons.append("market_data_unavailable")
        if not self.trading_api_ok:
            reasons.append("trading_api_unavailable")
        if self.short_volatility > settings.max_short_volatility:
            reasons.append("short_volatility_high")
        if self.absolute_window_move > settings.max_window_move:
            reasons.append("extreme_one_way_move")
        if self.trend_bid_depth < settings.min_exit_bid_depth:
            reasons.append("insufficient_exit_depth")
        if self.trend_spread > settings.max_spread:
            reasons.append("spread_too_wide")
        if not self.estimated_sellable:
            reasons.append("trend_position_not_sellable")
        return tuple(reasons)


@dataclass(frozen=True)
class TradePlan:
    round_id: int
    window_slug: str
    attempt: int
    making_amount: Decimal
    trend_side: Direction
    retained_side: Direction
    split_complete_set: bool = True


@dataclass(frozen=True)
class ExitInstruction:
    phase: str
    order_type: str
    limit_price: Decimal


@dataclass
class ActiveRound:
    round_id: int
    trend_side: Direction
    failures: int = 0
    awaiting_window: str | None = None
    committed: Decimal = Decimal("0")
    execution_phase: str = "idle"
    split_transaction_hash: str | None = None
    exit_sold_shares: Decimal = Decimal("0")
    exit_sell_proceeds: Decimal = Decimal("0")
    entry_submitted_price: Decimal | None = None
    planned_shares: Decimal = Decimal("0")
    entry_fees: Decimal = Decimal("0")
    cumulative_loss: Decimal = Decimal("0")
    entry_unmatched_attempts: int = 0
    soft_limit_final_recovery: bool = False

    @property
    def target_side(self) -> Direction:
        return self.trend_side.opposite


@dataclass
class PreparedSplit:
    window_slug: str
    amount: Decimal
    execution_phase: str = "planned"
    transaction_hash: str | None = None


@dataclass
class DailyMetrics:
    settled_windows: int = 0
    triggered_rounds: int = 0
    executed_rounds: int = 0
    successful_rounds: int = 0
    forced_exit_rounds: int = 0
    stage_successes: dict[str, int] = field(
        default_factory=lambda: {
            str(stage): 0 for stage in range(1, MAX_REVERSAL_STAGES + 1)
        }
    )
    maximum_same_direction_streak: int = 0
    total_making_amount: Decimal = Decimal("0")
    sell_proceeds: Decimal = Decimal("0")
    settlement_payout: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    unmatched_orders: int = 0
    net_profit: Decimal = Decimal("0")
    maximum_drawdown: Decimal = Decimal("0")
    volatility_pauses: int = 0
    api_order_errors: int = 0


@dataclass
class ReversalState:
    recent_results: list[Direction] = field(default_factory=list)
    recent_slugs: list[str] = field(default_factory=list)
    active_round: ActiveRound | None = None
    blocked_trend_side: Direction | None = None
    prepared_split: PreparedSplit | None = None
    last_opening_processed_slug: str | None = None
    next_round_id: int = 1
    current_streak_side: Direction | None = None
    current_streak: int = 0
    last_settled_slug: str | None = None
    chainlink_price_mode: str = "legacy"
    chainlink_open_prices: dict[str, Decimal] = field(default_factory=dict)
    pending_gamma_results: dict[str, Direction] = field(default_factory=dict)
    gamma_verified_slugs: list[str] = field(default_factory=list)
    gamma_mismatch_slugs: list[str] = field(default_factory=list)
    chain_verified_slugs: list[str] = field(default_factory=list)
    chain_mismatch_slugs: list[str] = field(default_factory=list)
    compact_consecutive_losing_rounds: int = 0
    compact_pause_windows_remaining: int = 0
    last_volatility_pause_slug: str | None = None
    reported_days: list[str] = field(default_factory=list)
    daily: dict[str, DailyMetrics] = field(default_factory=dict)


class ReversalV11:
    """Pure V1.1 strategy state machine; execution is delegated to a safe adapter."""

    def __init__(
        self,
        settings: ReversalSettings | None = None,
        state: ReversalState | None = None,
    ) -> None:
        self.settings = settings or ReversalSettings()
        self.state = state or ReversalState()

    def metrics(self, day: date | None = None) -> DailyMetrics:
        key = (day or datetime.now(timezone.utc).date()).isoformat()
        return self.state.daily.setdefault(key, DailyMetrics())

    def opening_split_amount(self, window_slug: str) -> Decimal:
        prepared = self.state.prepared_split
        if prepared is not None:
            if prepared.window_slug != window_slug:
                raise RuntimeError(
                    f"prepared split for {prepared.window_slug} must be reconciled first"
                )
            return prepared.amount
        active = self.state.active_round
        if active is None:
            return self.settings.stakes[0]
        if active.awaiting_window is not None and active.awaiting_window != window_slug:
            if active.execution_phase != "trend_exit_complete":
                raise RuntimeError(
                    "previous reversal attempt has not fully exited its trend-side position"
                )
            next_stage = active.failures + 1
            return (
                self.settings.stakes[next_stage]
                if next_stage < self.settings.attempt_limit
                else self.settings.stakes[0]
            )
        return self.settings.stakes[active.failures]

    def prepare_opening_split(self, window_slug: str) -> PreparedSplit:
        existing = self.state.prepared_split
        if existing is not None:
            if existing.window_slug != window_slug:
                raise RuntimeError(
                    f"prepared split for {existing.window_slug} must be reconciled first"
                )
            return existing
        prepared = PreparedSplit(
            window_slug=window_slug,
            amount=self.opening_split_amount(window_slug),
        )
        self.state.prepared_split = prepared
        return prepared

    def mark_opening_split_submitting(self) -> None:
        prepared = self._require_prepared_split()
        if prepared.execution_phase != "planned":
            raise RuntimeError(
                f"cannot submit opening split from phase {prepared.execution_phase}"
            )
        prepared.execution_phase = "split_submitting"

    def mark_opening_split_confirmed(self, transaction_hash: str) -> None:
        prepared = self._require_prepared_split()
        if prepared.execution_phase not in {"split_submitting", "split_confirmed"}:
            raise RuntimeError(
                f"cannot confirm opening split from phase {prepared.execution_phase}"
            )
        prepared.execution_phase = "split_confirmed"
        prepared.transaction_hash = transaction_hash

    def mark_opening_split_uncertain(self) -> None:
        self._require_prepared_split().execution_phase = "split_uncertain"

    def adopt_opening_split(
        self, plan: TradePlan, *, day: date | None = None
    ) -> None:
        prepared = self._require_prepared_split()
        if prepared.window_slug != plan.window_slug or prepared.amount != plan.making_amount:
            raise RuntimeError("opening split does not match the confirmed trade plan")
        if prepared.execution_phase != "split_confirmed":
            raise RuntimeError("opening split is not confirmed")
        active = self._require_active_plan(plan)
        if active.execution_phase != "planned":
            raise RuntimeError("trade plan cannot adopt an opening split from this phase")
        active.execution_phase = "split_confirmed"
        active.split_transaction_hash = prepared.transaction_hash
        if plan.attempt == 1:
            self.metrics(day).executed_rounds += 1
        self.state.last_opening_processed_slug = prepared.window_slug
        self.state.prepared_split = None

    def mark_opening_merge_submitting(self) -> None:
        prepared = self._require_prepared_split()
        if prepared.execution_phase != "split_confirmed":
            raise RuntimeError(
                f"cannot merge opening split from phase {prepared.execution_phase}"
            )
        prepared.execution_phase = "merge_submitting"

    def mark_opening_merge_uncertain(self) -> None:
        self._require_prepared_split().execution_phase = "merge_uncertain"

    def mark_opening_merge_confirmed(self) -> None:
        prepared = self._require_prepared_split()
        if prepared.execution_phase != "merge_submitting":
            raise RuntimeError(
                f"cannot confirm merge from phase {prepared.execution_phase}"
            )
        self.state.last_opening_processed_slug = prepared.window_slug
        self.state.prepared_split = None

    def roll_forward_uncertain_opening(
        self, current_window_slug: str, *, day: date | None = None
    ) -> str | None:
        """Abandon an older neutral complete-set attempt without blocking new windows."""
        prepared = self.state.prepared_split
        if (
            prepared is None
            or prepared.window_slug == current_window_slug
            or prepared.execution_phase not in {"split_uncertain", "merge_uncertain"}
        ):
            return None
        active = self.state.active_round
        if active is not None:
            if (
                active.awaiting_window != prepared.window_slug
                or active.execution_phase != "planned"
            ):
                raise RuntimeError(
                    "uncertain opening split does not match the planned active round"
                )
            self.state.active_round = None
        abandoned_slug = prepared.window_slug
        self.state.last_opening_processed_slug = abandoned_slug
        self.state.prepared_split = None
        self.metrics(day).api_order_errors += 1
        return abandoned_slug

    def _require_prepared_split(self) -> PreparedSplit:
        prepared = self.state.prepared_split
        if prepared is None:
            raise RuntimeError("no prepared opening split")
        return prepared

    def plan_window(
        self,
        window_slug: str,
        health: MarketHealth,
        *,
        day: date | None = None,
    ) -> TradePlan | None:
        active = self.state.active_round
        if active is not None and active.awaiting_window is not None:
            if active.awaiting_window != window_slug:
                return None
            return TradePlan(
                round_id=active.round_id,
                window_slug=window_slug,
                attempt=active.failures + 1,
                making_amount=(
                    active.planned_shares
                    if active.planned_shares > 0
                    else self.settings.stakes[active.failures]
                ),
                trend_side=active.trend_side,
                retained_side=active.target_side,
            )
        first_stage_trigger_ready = (
            active is None
            and len(self.state.recent_results) >= self.settings.trigger_streak
            and len(set(self.state.recent_results[-self.settings.trigger_streak :])) == 1
            and _are_immediate_predecessors(
                self.state.recent_slugs,
                window_slug,
                self.settings.trigger_streak,
            )
            and self.state.blocked_trend_side != self.state.recent_results[-1]
        )
        if first_stage_trigger_ready and self.settings.first_stage_volatility_block_reason(
            health.short_volatility,
            health.five_minute_volatility,
        ) is not None:
            if self.state.last_volatility_pause_slug != window_slug:
                self.metrics(day).volatility_pauses += 1
                self.state.last_volatility_pause_slug = window_slug
            return None
        reasons = (
            health.blocked_reasons(self.settings)
            if self.settings.market_filters_enabled
            else ()
        )
        if reasons:
            if (
                "short_volatility_high" in reasons
                or "extreme_one_way_move" in reasons
            ) and self.state.last_volatility_pause_slug != window_slug:
                self.metrics(day).volatility_pauses += 1
                self.state.last_volatility_pause_slug = window_slug
            return None
        if active is None:
            if (
                self.settings.compact_two_stage_enabled
                and self.state.compact_pause_windows_remaining > 0
            ):
                return None
            if (
                len(self.state.recent_results) < self.settings.trigger_streak
                or len(
                    set(self.state.recent_results[-self.settings.trigger_streak :])
                )
                != 1
                or not _are_immediate_predecessors(
                    self.state.recent_slugs,
                    window_slug,
                    self.settings.trigger_streak,
                )
            ):
                return None
            if self.state.blocked_trend_side == self.state.recent_results[-1]:
                return None
            active = ActiveRound(
                round_id=self.state.next_round_id,
                trend_side=self.state.recent_results[-1],
            )
            self.state.next_round_id += 1
            self.state.active_round = active
            self.metrics(day).triggered_rounds += 1

        if active.failures >= self.settings.attempt_limit:
            raise RuntimeError("round exceeded the configured attempt limit")
        amount = self.settings.stakes[active.failures]
        if (
            self.settings.one_final_recovery_after_soft_limit
            and self.settings.soft_round_loss_limit is not None
            and active.cumulative_loss >= self.settings.soft_round_loss_limit
        ):
            active.soft_limit_final_recovery = True
        active.awaiting_window = window_slug
        active.committed += amount
        active.execution_phase = "planned"
        active.split_transaction_hash = None
        active.exit_sold_shares = Decimal("0")
        active.exit_sell_proceeds = Decimal("0")
        active.entry_submitted_price = None
        active.planned_shares = amount
        active.entry_fees = Decimal("0")
        self.metrics(day).total_making_amount += amount
        return TradePlan(
            round_id=active.round_id,
            window_slug=window_slug,
            attempt=active.failures + 1,
            making_amount=amount,
            trend_side=active.trend_side,
            retained_side=active.target_side,
        )

    def settle_window(
        self,
        window_slug: str,
        result: Direction | str,
        *,
        day: date | None = None,
    ) -> str:
        result = Direction(result)
        compact_pause_before = self.state.compact_pause_windows_remaining
        if window_slug == self.state.last_settled_slug:
            return "duplicate_ignored"
        previous_epoch: int | None = None
        current_epoch = int(window_slug.rpartition("-")[2])
        if self.state.last_settled_slug is not None:
            previous_epoch = int(self.state.last_settled_slug.rpartition("-")[2])
            if current_epoch <= previous_epoch:
                return "duplicate_ignored"
        continuous = previous_epoch is None or current_epoch == previous_epoch + 300
        if not continuous:
            self.state.recent_results = []
            self.state.recent_slugs = []
            self.state.current_streak_side = None
            self.state.current_streak = 0
        self.state.last_settled_slug = window_slug
        self.state.recent_results = (
            self.state.recent_results + [result]
        )[-self.settings.trigger_streak :]
        self.state.recent_slugs = (
            self.state.recent_slugs + [window_slug]
        )[-self.settings.trigger_streak :]
        metrics = self.metrics(day)
        metrics.settled_windows += 1
        if self.state.current_streak_side == result:
            self.state.current_streak += 1
        else:
            self.state.current_streak_side = result
            self.state.current_streak = 1
        if (
            self.state.blocked_trend_side is not None
            and result != self.state.blocked_trend_side
        ):
            self.state.blocked_trend_side = None
        metrics.maximum_same_direction_streak = max(
            metrics.maximum_same_direction_streak,
            self.state.current_streak,
        )
        if self.settings.compact_two_stage_enabled and compact_pause_before > 0:
            self.state.compact_pause_windows_remaining = compact_pause_before - 1

        active = self.state.active_round
        if active is None or active.awaiting_window != window_slug:
            return "observed"
        active.awaiting_window = None
        direct_buy = active.split_transaction_hash == "direct-buy"
        acquired_shares = (
            active.exit_sold_shares
            if direct_buy
            else self.settings.stakes[active.failures]
        )
        if direct_buy:
            planned_shares = (
                active.planned_shares
                if active.planned_shares > 0
                else self.settings.stakes[active.failures]
            )
            unfilled = max(
                Decimal("0"),
                planned_shares - acquired_shares,
            )
            metrics.total_making_amount -= unfilled
            if acquired_shares <= 0:
                metrics.net_profit = (
                    metrics.sell_proceeds
                    + metrics.settlement_payout
                    - metrics.total_making_amount
                    - metrics.fees
                    - metrics.slippage
                )
                self.state.active_round = None
                return "direct_entry_unfilled"
        if result == active.target_side:
            metrics.successful_rounds += 1
            metrics.stage_successes[str(active.failures + 1)] += 1
            metrics.settlement_payout += acquired_shares
            metrics.net_profit = (
                metrics.sell_proceeds
                + metrics.settlement_payout
                - metrics.total_making_amount
                - metrics.fees
                - metrics.slippage
            )
            self.state.active_round = None
            self.state.blocked_trend_side = None
            if self.settings.compact_two_stage_enabled:
                self.state.compact_consecutive_losing_rounds = 0
            return "reversal_success"

        completed_attempt = active.failures + 1
        active.failures += 1
        if direct_buy and self.settings.tracks_direct_loss(completed_attempt):
            active.cumulative_loss += active.exit_sell_proceeds + active.entry_fees
        if (
            self.settings.end_round_at_soft_loss_limit
            and self.settings.soft_round_loss_limit is not None
            and active.cumulative_loss >= self.settings.soft_round_loss_limit
        ):
            metrics.forced_exit_rounds += 1
            self.state.blocked_trend_side = active.trend_side
            self.state.active_round = None
            metrics.net_profit = (
                metrics.sell_proceeds
                + metrics.settlement_payout
                - metrics.total_making_amount
                - metrics.fees
                - metrics.slippage
            )
            return "soft_loss_limit_reached"
        if (
            active.soft_limit_final_recovery
            and not self.settings.continue_final_stage_until_success_or_unfunded
        ):
            metrics.forced_exit_rounds += 1
            self.state.blocked_trend_side = active.trend_side
            self.state.active_round = None
            metrics.net_profit = (
                metrics.sell_proceeds
                + metrics.settlement_payout
                - metrics.total_making_amount
                - metrics.fees
                - metrics.slippage
            )
            return "soft_limit_final_recovery_loss"
        if (
            active.failures >= self.settings.attempt_limit
            and self.settings.continue_final_stage_until_success_or_unfunded
        ):
            # The named stage limit caps the progression and notification stage;
            # it does not abandon a funded live round. Keep retrying the final
            # full-recovery stage until the reversal wins or funding is short.
            active.failures = self.settings.attempt_limit - 1
            active.execution_phase = "idle"
            active.split_transaction_hash = None
            active.exit_sold_shares = Decimal("0")
            active.exit_sell_proceeds = Decimal("0")
            active.entry_submitted_price = None
            active.planned_shares = Decimal("0")
            active.entry_fees = Decimal("0")
            active.entry_unmatched_attempts = 0
            return "trend_continued_at_final_stage"
        if active.failures >= self.settings.attempt_limit:
            metrics.forced_exit_rounds += 1
            self.state.blocked_trend_side = active.trend_side
            self.state.active_round = None
            if self.settings.compact_two_stage_enabled:
                self._record_compact_losing_round()
            metrics.net_profit = (
                metrics.sell_proceeds
                + metrics.settlement_payout
                - metrics.total_making_amount
                - metrics.fees
                - metrics.slippage
            )
            return (
                "compact_two_stage_loss"
                if self.settings.compact_two_stage_enabled
                else "first_stage_only_loss"
                if self.settings.first_stage_only_enabled
                else "fixed_notional_stages_loss"
                if self.settings.fixed_notional_stages
                else "forced_exit_after_twenty_five_failures"
                if self.settings.attempt_limit == MAX_REVERSAL_STAGES
                else "forced_exit_after_configured_failures"
            )
        active.execution_phase = "idle"
        active.split_transaction_hash = None
        active.exit_sold_shares = Decimal("0")
        active.exit_sell_proceeds = Decimal("0")
        active.entry_submitted_price = None
        active.planned_shares = Decimal("0")
        active.entry_fees = Decimal("0")
        active.entry_unmatched_attempts = 0
        return "trend_continued"

    def reconcile_stale_direct_entry(
        self,
        window_slug: str,
        result: Direction | str,
        *,
        day: date | None = None,
    ) -> str:
        """Close a direct-buy round whose result cursor has already passed it.

        This is a recovery path for a process that was suspended across several
        windows.  It never creates a catch-up trade and is deliberately limited
        to a fully completed direct entry, where no trend-side position exists.
        """
        active = self.state.active_round
        if active is None or active.awaiting_window != window_slug:
            return "stale_active_absent"
        if (
            active.split_transaction_hash != "direct-buy"
            or active.execution_phase != "direct_entry_complete"
        ):
            raise RuntimeError(
                "stale reversal round cannot be reconciled without position review"
            )
        if self.state.last_settled_slug is None:
            return "stale_active_not_passed"
        active_epoch = int(window_slug.rpartition("-")[2])
        last_epoch = int(self.state.last_settled_slug.rpartition("-")[2])
        if active_epoch >= last_epoch:
            return "stale_active_not_passed"

        result = Direction(result)
        metrics = self.metrics(day)
        metrics.settled_windows += 1
        acquired_shares = active.exit_sold_shares
        if result == active.target_side:
            metrics.successful_rounds += 1
            metrics.stage_successes[str(active.failures + 1)] += 1
            metrics.settlement_payout += acquired_shares
            outcome = "stale_reversal_success"
        else:
            metrics.forced_exit_rounds += 1
            outcome = "stale_reversal_loss_closed"
        metrics.net_profit = (
            metrics.sell_proceeds
            + metrics.settlement_payout
            - metrics.total_making_amount
            - metrics.fees
            - metrics.slippage
        )
        self.state.active_round = None
        self.state.blocked_trend_side = None
        return outcome

    def resize_active_plan(self, plan: TradePlan, shares: Decimal) -> TradePlan:
        if shares <= 0:
            raise ValueError("resized plan shares must be positive")
        active = self._require_active_plan(plan)
        if active.execution_phase not in {
            "planned",
            "direct_entry_ready",
            "direct_entry_partial",
        }:
            raise RuntimeError("only an unsubmitted/partial direct attempt can be resized")
        difference = shares - plan.making_amount
        active.committed += difference
        active.planned_shares = shares
        self.metrics().total_making_amount += difference
        return TradePlan(
            round_id=plan.round_id,
            window_slug=plan.window_slug,
            attempt=plan.attempt,
            making_amount=shares,
            trend_side=plan.trend_side,
            retained_side=plan.retained_side,
            split_complete_set=False,
        )

    def mark_observation_stage(self, plan: TradePlan) -> None:
        active = self._require_active_plan(plan)
        if plan.making_amount != 0 or active.execution_phase != "planned":
            raise RuntimeError("only a planned zero-notional stage can be observed")
        active.execution_phase = "observation_only"
        self.state.last_opening_processed_slug = plan.window_slug

    def abandon_filtered_attempt(
        self,
        plan: TradePlan,
        *,
        block_trend: bool = True,
    ) -> None:
        active = self._require_active_plan(plan)
        if (
            active.execution_phase not in {"planned", "direct_entry_ready"}
            or active.exit_sold_shares > 0
        ):
            raise RuntimeError(
                "only an unfilled planned/ready filtered attempt can be abandoned"
            )
        self.metrics().total_making_amount -= plan.making_amount
        self.metrics().forced_exit_rounds += 1
        self.metrics().net_profit = (
            self.metrics().sell_proceeds
            + self.metrics().settlement_payout
            - self.metrics().total_making_amount
            - self.metrics().fees
            - self.metrics().slippage
        )
        if block_trend:
            self.state.blocked_trend_side = active.trend_side
        self.state.last_opening_processed_slug = plan.window_slug
        if self.settings.compact_two_stage_enabled and active.failures > 0:
            self._record_compact_losing_round()
        self.state.active_round = None

    def defer_unfilled_attempt(self, plan: TradePlan) -> None:
        """Skip this window without consuming the stage or ending its round."""
        active = self._require_active_plan(plan)
        if (
            active.execution_phase not in {"planned", "direct_entry_ready"}
            or active.exit_sold_shares > 0
        ):
            raise RuntimeError(
                "only an unfilled planned/ready attempt can be deferred"
            )
        active.committed -= plan.making_amount
        self.metrics().total_making_amount -= plan.making_amount
        active.awaiting_window = None
        active.execution_phase = "idle"
        active.split_transaction_hash = None
        active.exit_sold_shares = Decimal("0")
        active.exit_sell_proceeds = Decimal("0")
        active.entry_submitted_price = None
        active.planned_shares = Decimal("0")
        active.entry_fees = Decimal("0")
        active.entry_unmatched_attempts = 0
        self.state.last_opening_processed_slug = plan.window_slug

    def record_direct_entry_unmatched(self, plan: TradePlan) -> int:
        active = self._require_active_plan(plan)
        if active.exit_sold_shares > 0:
            return active.entry_unmatched_attempts
        active.entry_unmatched_attempts += 1
        return active.entry_unmatched_attempts

    def abandon_dynamic_recovery(self, plan: TradePlan) -> None:
        self.abandon_filtered_attempt(plan)

    def _record_compact_losing_round(self) -> None:
        self.state.compact_consecutive_losing_rounds += 1
        if (
            self.state.compact_consecutive_losing_rounds
            >= self.settings.compact_loss_round_limit
        ):
            self.state.compact_consecutive_losing_rounds = 0
            self.state.compact_pause_windows_remaining = (
                self.settings.compact_pause_windows
            )

    def mark_direct_entry_ready(
        self,
        plan: TradePlan,
        *,
        day: date | None = None,
    ) -> None:
        active = self._require_active_plan(plan)
        if active.execution_phase != "planned":
            raise RuntimeError(
                f"cannot prepare direct entry from phase {active.execution_phase}"
            )
        active.execution_phase = "direct_entry_ready"
        active.split_transaction_hash = "direct-buy"
        self.state.last_opening_processed_slug = plan.window_slug

    def mark_direct_entry_submitting(self, plan: TradePlan, price: Decimal) -> None:
        active = self._require_active_plan(plan)
        if active.execution_phase not in {"direct_entry_ready", "direct_entry_partial"}:
            raise RuntimeError(
                f"cannot submit direct entry from phase {active.execution_phase}"
            )
        active.execution_phase = "direct_entry_submitting"
        active.entry_submitted_price = price

    def mark_direct_entry_retryable(self, plan: TradePlan) -> None:
        active = self._require_active_plan(plan)
        if active.execution_phase != "direct_entry_submitting":
            raise RuntimeError(
                f"cannot retry direct entry from phase {active.execution_phase}"
            )
        active.execution_phase = (
            "direct_entry_partial"
            if active.exit_sold_shares > 0
            else "direct_entry_ready"
        )
        active.entry_submitted_price = None

    def record_direct_entry_fill(
        self,
        plan: TradePlan,
        *,
        shares: Decimal,
        cost: Decimal,
        day: date | None = None,
    ) -> bool:
        if shares < 0 or cost < 0:
            raise ValueError("direct entry fill amounts must not be negative")
        active = self._require_active_plan(plan)
        if active.execution_phase not in {
            "direct_entry_ready",
            "direct_entry_partial",
            "direct_entry_submitting",
        }:
            raise RuntimeError(
                f"cannot record direct entry from phase {active.execution_phase}"
            )
        planned_remaining = max(
            Decimal("0"), plan.making_amount - active.exit_sold_shares
        )
        # BUY limit orders reserve their limit-price notional. A better fill
        # price can return slightly more tokens than the nominal share count.
        accepted_shares = shares
        accepted_cost = cost
        first_fill = active.exit_sold_shares <= 0 and accepted_shares > 0
        placeholder_replaced = min(planned_remaining, accepted_shares)
        active.exit_sold_shares += accepted_shares
        active.exit_sell_proceeds += accepted_cost
        if accepted_shares > 0:
            average_price = accepted_cost / accepted_shares
            accepted_fee = (
                accepted_shares
                * Decimal("0.07")
                * average_price
                * (Decimal("1") - average_price)
            )
            active.entry_fees += accepted_fee
            self.metrics(day).fees += accepted_fee
        if first_fill and plan.attempt == 1:
            self.metrics(day).executed_rounds += 1
        active.entry_submitted_price = None
        active.execution_phase = (
            "direct_entry_complete"
            if active.exit_sold_shares >= plan.making_amount
            else "direct_entry_partial"
        )
        # plan_window reserves one pUSD per planned share. Replace the filled
        # portion of that placeholder with its actual CLOB purchase cost.
        self.metrics(day).total_making_amount += accepted_cost - placeholder_replaced
        return active.execution_phase == "direct_entry_complete"

    def mark_split_confirmed(self, plan: TradePlan, transaction_hash: str) -> None:
        active = self._require_active_plan(plan)
        if active.execution_phase not in {"split_submitting", "split_confirmed"}:
            raise RuntimeError(f"cannot confirm split from phase {active.execution_phase}")
        active.execution_phase = "split_confirmed"
        active.split_transaction_hash = transaction_hash

    def mark_split_submitting(self, plan: TradePlan) -> None:
        active = self._require_active_plan(plan)
        if active.execution_phase != "planned":
            raise RuntimeError(f"cannot submit split from phase {active.execution_phase}")
        active.execution_phase = "split_submitting"

    def mark_split_uncertain(self, plan: TradePlan) -> None:
        active = self._require_active_plan(plan)
        active.execution_phase = "split_uncertain"

    def record_exit_fill(
        self,
        plan: TradePlan,
        *,
        shares: Decimal,
        proceeds: Decimal,
        fees: Decimal = Decimal("0"),
        slippage: Decimal = Decimal("0"),
        day: date | None = None,
    ) -> bool:
        if shares < 0 or proceeds < 0 or fees < 0 or slippage < 0:
            raise ValueError("exit fill amounts must not be negative")
        active = self._require_active_plan(plan)
        if active.execution_phase not in {
            "split_confirmed",
            "trend_exit_partial",
            "trend_exit_submitting",
        }:
            raise RuntimeError(f"cannot record exit from phase {active.execution_phase}")
        remaining = max(Decimal("0"), plan.making_amount - active.exit_sold_shares)
        accepted_shares = min(remaining, shares)
        accepted_proceeds = (
            proceeds * accepted_shares / shares if shares > 0 else Decimal("0")
        )
        active.exit_sold_shares += accepted_shares
        active.exit_sell_proceeds += accepted_proceeds
        active.execution_phase = (
            "trend_exit_complete"
            if active.exit_sold_shares >= plan.making_amount
            else "trend_exit_partial"
        )
        self.record_execution(
            sell_proceeds=accepted_proceeds,
            fees=fees,
            slippage=slippage,
            day=day,
        )
        return active.execution_phase == "trend_exit_complete"

    def mark_exit_submitting(self, plan: TradePlan) -> None:
        active = self._require_active_plan(plan)
        if active.execution_phase not in {"split_confirmed", "trend_exit_partial"}:
            raise RuntimeError(f"cannot submit exit from phase {active.execution_phase}")
        active.execution_phase = "trend_exit_submitting"

    def mark_exit_retryable(self, plan: TradePlan) -> None:
        active = self._require_active_plan(plan)
        if active.execution_phase != "trend_exit_submitting":
            raise RuntimeError(f"cannot retry exit from phase {active.execution_phase}")
        active.execution_phase = (
            "trend_exit_partial"
            if active.exit_sold_shares > 0
            else "split_confirmed"
        )

    def reconcile_exit_complete(self, plan: TradePlan) -> None:
        active = self._require_active_plan(plan)
        if active.execution_phase not in {
            "split_confirmed",
            "trend_exit_partial",
            "trend_exit_submitting",
        }:
            raise RuntimeError(f"cannot reconcile exit from phase {active.execution_phase}")
        active.exit_sold_shares = plan.making_amount
        active.execution_phase = "trend_exit_complete"

    def _require_active_plan(self, plan: TradePlan) -> ActiveRound:
        active = self.state.active_round
        if (
            active is None
            or active.round_id != plan.round_id
            or active.awaiting_window != plan.window_slug
        ):
            raise RuntimeError("trade plan is not the active V1.1 attempt")
        return active

    def record_execution(
        self,
        *,
        sell_proceeds: Decimal = Decimal("0"),
        settlement_payout: Decimal = Decimal("0"),
        fees: Decimal = Decimal("0"),
        slippage: Decimal = Decimal("0"),
        unmatched_orders: int = 0,
        api_order_errors: int = 0,
        equity_drawdown: Decimal = Decimal("0"),
        day: date | None = None,
    ) -> None:
        metrics = self.metrics(day)
        metrics.sell_proceeds += sell_proceeds
        metrics.settlement_payout += settlement_payout
        metrics.fees += fees
        metrics.slippage += slippage
        metrics.unmatched_orders += unmatched_orders
        metrics.api_order_errors += api_order_errors
        metrics.maximum_drawdown = max(metrics.maximum_drawdown, equity_drawdown)
        metrics.net_profit = (
            metrics.sell_proceeds
            + metrics.settlement_payout
            - metrics.total_making_amount
            - metrics.fees
            - metrics.slippage
        )

    def quarantine_gamma_mismatch(
        self,
        slug: str,
        *,
        day: date | None = None,
    ) -> None:
        """Remove one conflicting result from retries while retaining an audit trail."""
        self.state.pending_gamma_results.pop(slug, None)
        self.state.gamma_mismatch_slugs = (
            [value for value in self.state.gamma_mismatch_slugs if value != slug]
            + [slug]
        )[-100:]
        self.metrics(day).api_order_errors += 1

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _jsonable(asdict(self.state))
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    @classmethod
    def load(
        cls,
        path: Path,
        settings: ReversalSettings | None = None,
    ) -> "ReversalV11":
        if not path.exists():
            return cls(settings=settings)
        payload = json.loads(path.read_text(encoding="utf-8"))
        active_payload = payload.get("active_round")
        active = (
            ActiveRound(
                round_id=int(active_payload["round_id"]),
                trend_side=Direction(active_payload["trend_side"]),
                failures=int(active_payload.get("failures", 0)),
                awaiting_window=active_payload.get("awaiting_window"),
                committed=Decimal(str(active_payload.get("committed", "0"))),
                execution_phase=str(active_payload.get("execution_phase", "idle")),
                split_transaction_hash=active_payload.get("split_transaction_hash"),
                exit_sold_shares=Decimal(
                    str(active_payload.get("exit_sold_shares", "0"))
                ),
                exit_sell_proceeds=Decimal(
                    str(active_payload.get("exit_sell_proceeds", "0"))
                ),
                entry_submitted_price=(
                    Decimal(str(active_payload["entry_submitted_price"]))
                    if active_payload.get("entry_submitted_price") is not None
                    else None
                ),
                planned_shares=Decimal(
                    str(active_payload.get("planned_shares", "0"))
                ),
                entry_fees=Decimal(str(active_payload.get("entry_fees", "0"))),
                cumulative_loss=Decimal(
                    str(active_payload.get("cumulative_loss", "0"))
                ),
                entry_unmatched_attempts=int(
                    active_payload.get("entry_unmatched_attempts", 0)
                ),
                soft_limit_final_recovery=bool(
                    active_payload.get("soft_limit_final_recovery", False)
                ),
            )
            if isinstance(active_payload, dict)
            else None
        )
        daily = {
            str(key): DailyMetrics(
                settled_windows=int(value.get("settled_windows", 0)),
                triggered_rounds=int(value.get("triggered_rounds", 0)),
                executed_rounds=int(
                    value.get(
                        "executed_rounds",
                        int(value.get("successful_rounds", 0))
                        + int(value.get("forced_exit_rounds", 0)),
                    )
                ),
                successful_rounds=int(value.get("successful_rounds", 0)),
                forced_exit_rounds=int(value.get("forced_exit_rounds", 0)),
                stage_successes={
                    str(stage): int(
                        (value.get("stage_successes") or {}).get(str(stage), 0)
                    )
                    for stage in range(1, MAX_REVERSAL_STAGES + 1)
                },
                maximum_same_direction_streak=int(
                    value.get("maximum_same_direction_streak", 0)
                ),
                total_making_amount=Decimal(str(value.get("total_making_amount", "0"))),
                sell_proceeds=Decimal(str(value.get("sell_proceeds", "0"))),
                settlement_payout=Decimal(str(value.get("settlement_payout", "0"))),
                fees=Decimal(str(value.get("fees", "0"))),
                slippage=Decimal(str(value.get("slippage", "0"))),
                unmatched_orders=int(value.get("unmatched_orders", 0)),
                net_profit=Decimal(str(value.get("net_profit", "0"))),
                maximum_drawdown=Decimal(str(value.get("maximum_drawdown", "0"))),
                volatility_pauses=int(value.get("volatility_pauses", 0)),
                api_order_errors=int(value.get("api_order_errors", 0)),
            )
            for key, value in (payload.get("daily") or {}).items()
            if isinstance(value, dict)
        }
        state = ReversalState(
            recent_results=[Direction(value) for value in payload.get("recent_results", [])],
            recent_slugs=[str(value) for value in payload.get("recent_slugs", [])],
            active_round=active,
            blocked_trend_side=(
                Direction(payload["blocked_trend_side"])
                if payload.get("blocked_trend_side")
                else None
            ),
            prepared_split=(
                PreparedSplit(
                    window_slug=str(payload["prepared_split"]["window_slug"]),
                    amount=Decimal(str(payload["prepared_split"]["amount"])),
                    execution_phase=str(
                        payload["prepared_split"].get("execution_phase", "planned")
                    ),
                    transaction_hash=payload["prepared_split"].get("transaction_hash"),
                )
                if isinstance(payload.get("prepared_split"), dict)
                else None
            ),
            last_opening_processed_slug=payload.get("last_opening_processed_slug"),
            next_round_id=int(payload.get("next_round_id", 1)),
            current_streak_side=(
                Direction(payload["current_streak_side"])
                if payload.get("current_streak_side")
                else None
            ),
            current_streak=int(payload.get("current_streak", 0)),
            last_settled_slug=payload.get("last_settled_slug"),
            chainlink_price_mode=(
                "twap60"
                if str(payload.get("chainlink_price_mode", "legacy")) == "twap30"
                else str(payload.get("chainlink_price_mode", "legacy"))
            ),
            chainlink_open_prices={
                str(slug): Decimal(str(price))
                for slug, price in (payload.get("chainlink_open_prices") or {}).items()
            },
            pending_gamma_results={
                str(slug): Direction(result)
                for slug, result in (payload.get("pending_gamma_results") or {}).items()
            },
            gamma_verified_slugs=[
                str(value) for value in payload.get("gamma_verified_slugs", [])
            ],
            gamma_mismatch_slugs=[
                str(value) for value in payload.get("gamma_mismatch_slugs", [])
            ],
            chain_verified_slugs=[
                str(value) for value in payload.get("chain_verified_slugs", [])
            ],
            chain_mismatch_slugs=[
                str(value) for value in payload.get("chain_mismatch_slugs", [])
            ],
            compact_consecutive_losing_rounds=int(
                payload.get("compact_consecutive_losing_rounds", 0)
            ),
            compact_pause_windows_remaining=int(
                payload.get("compact_pause_windows_remaining", 0)
            ),
            last_volatility_pause_slug=payload.get("last_volatility_pause_slug"),
            reported_days=[str(value) for value in payload.get("reported_days", [])],
            daily=daily,
        )
        _normalize_recent_continuity(state)
        return cls(settings=settings, state=state)

    def execute_complete_set(
        self,
        plan: TradePlan,
        splitter: CompleteSetSplitter,
        *,
        condition_id: str,
        up_token_id: str,
        down_token_id: str,
        neg_risk: bool,
    ) -> SplitReceipt:
        active = self.state.active_round
        if (
            active is None
            or active.round_id != plan.round_id
            or active.awaiting_window != plan.window_slug
        ):
            raise RuntimeError("trade plan is not the active V1.1 attempt")
        return splitter.split(
            condition_id=condition_id,
            up_token_id=up_token_id,
            down_token_id=down_token_id,
            amount=plan.making_amount,
            neg_risk=neg_risk,
        )


def build_exit_sequence(
    best_bid: Decimal,
    tick_size: Decimal,
    *,
    preferred_price: Decimal = Decimal("0.50"),
) -> tuple[ExitInstruction, ...]:
    if best_bid <= 0 or tick_size <= 0:
        raise ValueError("best bid and tick size must be positive")
    executable = max(tick_size, best_bid - tick_size)
    if best_bid >= preferred_price:
        return (
            ExitInstruction("preferred_immediate", "FAK", best_bid),
            ExitInstruction("reprice_best_bid", "GTC", executable),
            ExitInstruction("final_immediate", "FAK", executable),
        )
    return (
        ExitInstruction("preferred_resting", "GTC", preferred_price),
        ExitInstruction("reprice_best_bid", "GTC", executable),
        ExitInstruction("final_immediate", "FAK", executable),
    )


def format_daily_report(
    report_day: date,
    metrics: DailyMetrics,
    maximum_attempts: int = MAX_REVERSAL_STAGES,
) -> str:
    stages = " / ".join(
        f"第{stage}次 {metrics.stage_successes.get(str(stage), 0)}"
        for stage in range(1, maximum_attempts + 1)
    )
    return "\n".join(
        [
            f"📊 BTC 5分钟反转策略 V1.1 日报 {report_day.isoformat()}",
            f"总结算窗口: {metrics.settled_windows}",
            f"信号触发轮数: {metrics.triggered_rounds}",
            f"已确认拆分轮数: {metrics.executed_rounds}",
            f"成功轮数: {metrics.successful_rounds}",
            f"{'二十五' if maximum_attempts == 25 else maximum_attempts}次失败退出: "
            f"{metrics.forced_exit_rounds}",
            f"各阶段成功: {stages}",
            f"最大连续同向窗口: {metrics.maximum_same_direction_streak}",
            f"总做市金额: {metrics.total_making_amount}U",
            f"卖出回款: {metrics.sell_proceeds}U",
            f"结算收益: {metrics.settlement_payout}U",
            f"手续费: {metrics.fees}U",
            f"滑点: {metrics.slippage}U",
            f"未成交订单: {metrics.unmatched_orders}",
            f"当日净收益: {metrics.net_profit}U",
            f"当日最大回撤: {metrics.maximum_drawdown}U",
            f"波动率暂停: {metrics.volatility_pauses}",
            f"API及下单异常: {metrics.api_order_errors}",
        ]
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _are_immediate_predecessors(
    recent_slugs: list[str],
    window_slug: str,
    count: int = 2,
) -> bool:
    if count < 1 or len(recent_slugs) < count:
        return False
    try:
        current_epoch = int(window_slug.rpartition("-")[2])
        recent_epochs = [
            int(slug.rpartition("-")[2]) for slug in recent_slugs[-count:]
        ]
    except ValueError:
        return False
    return recent_epochs == [
        current_epoch - 300 * offset for offset in range(count, 0, -1)
    ]


def _normalize_recent_continuity(state: ReversalState) -> None:
    """Trim persisted trend history to its trailing gap-free sequence."""
    pair_count = min(len(state.recent_slugs), len(state.recent_results))
    pairs = list(
        zip(
            state.recent_slugs[-pair_count:],
            state.recent_results[-pair_count:],
        )
    )
    if not pairs or pairs[-1][0] != state.last_settled_slug:
        state.recent_slugs = []
        state.recent_results = []
        state.current_streak_side = None
        state.current_streak = 0
        return
    start = len(pairs) - 1
    while start > 0:
        previous_epoch = int(pairs[start - 1][0].rpartition("-")[2])
        current_epoch = int(pairs[start][0].rpartition("-")[2])
        if current_epoch != previous_epoch + 300:
            break
        start -= 1
    if start == 0:
        return
    trailing = pairs[start:]
    state.recent_slugs = [slug for slug, _ in trailing]
    state.recent_results = [result for _, result in trailing]
    final_side = trailing[-1][1]
    streak = 0
    for _, result in reversed(trailing):
        if result is not final_side:
            break
        streak += 1
    state.current_streak_side = final_side
    state.current_streak = streak
