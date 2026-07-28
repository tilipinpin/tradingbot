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


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"

    @property
    def opposite(self) -> "Direction":
        return Direction.DOWN if self is Direction.UP else Direction.UP


@dataclass(frozen=True)
class ReversalSettings:
    stakes: tuple[Decimal, ...] = (
        Decimal("2"),
        Decimal("4"),
        Decimal("8"),
        Decimal("16"),
    )
    preferred_sell_price: Decimal = Decimal("0.50")
    max_short_volatility: Decimal = Decimal("0.0015")
    max_window_move: Decimal = Decimal("0.0030")
    min_exit_bid_depth: Decimal = Decimal("2")
    max_spread: Decimal = Decimal("0.05")
    market_filters_enabled: bool = False

    def __post_init__(self) -> None:
        if self.stakes != tuple(Decimal(2**index) * Decimal("2") for index in range(4)):
            raise ValueError("V1.1 stakes must be exactly 2, 4, 8, 16")
        if any(value <= 0 for value in self.stakes):
            raise ValueError("stakes must be positive")
        if sum(self.stakes) != Decimal("30"):
            raise ValueError("V1.1 maximum round commitment must be 30U")
        if not Decimal("0") < self.preferred_sell_price < Decimal("1"):
            raise ValueError("preferred sell price must be between zero and one")


@dataclass(frozen=True)
class MarketHealth:
    short_volatility: Decimal
    absolute_window_move: Decimal
    trend_bid_depth: Decimal
    trend_spread: Decimal
    estimated_sellable: bool
    market_data_ok: bool = True
    trading_api_ok: bool = True

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
    successful_rounds: int = 0
    forced_exit_rounds: int = 0
    stage_successes: dict[str, int] = field(
        default_factory=lambda: {"1": 0, "2": 0, "3": 0, "4": 0}
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
    prepared_split: PreparedSplit | None = None
    last_opening_processed_slug: str | None = None
    next_round_id: int = 1
    current_streak_side: Direction | None = None
    current_streak: int = 0
    last_settled_slug: str | None = None
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
                if next_stage < len(self.settings.stakes)
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

    def adopt_opening_split(self, plan: TradePlan) -> None:
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
                making_amount=self.settings.stakes[active.failures],
                trend_side=active.trend_side,
                retained_side=active.target_side,
            )
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
                len(self.state.recent_results) < 2
                or self.state.recent_results[-1] != self.state.recent_results[-2]
                or not _are_immediate_predecessors(
                    self.state.recent_slugs,
                    window_slug,
                )
            ):
                return None
            active = ActiveRound(
                round_id=self.state.next_round_id,
                trend_side=self.state.recent_results[-1],
            )
            self.state.next_round_id += 1
            self.state.active_round = active
            self.metrics(day).triggered_rounds += 1

        if active.failures >= len(self.settings.stakes):
            raise RuntimeError("round exceeded the four-attempt safety limit")
        amount = self.settings.stakes[active.failures]
        active.awaiting_window = window_slug
        active.committed += amount
        active.execution_phase = "planned"
        active.split_transaction_hash = None
        active.exit_sold_shares = Decimal("0")
        active.exit_sell_proceeds = Decimal("0")
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
        if window_slug == self.state.last_settled_slug:
            return "duplicate_ignored"
        self.state.last_settled_slug = window_slug
        self.state.recent_results = (self.state.recent_results + [result])[-2:]
        self.state.recent_slugs = (self.state.recent_slugs + [window_slug])[-2:]
        metrics = self.metrics(day)
        metrics.settled_windows += 1
        if self.state.current_streak_side == result:
            self.state.current_streak += 1
        else:
            self.state.current_streak_side = result
            self.state.current_streak = 1
        metrics.maximum_same_direction_streak = max(
            metrics.maximum_same_direction_streak,
            self.state.current_streak,
        )

        active = self.state.active_round
        if active is None or active.awaiting_window != window_slug:
            return "observed"
        active.awaiting_window = None
        if result == active.target_side:
            metrics.successful_rounds += 1
            metrics.stage_successes[str(active.failures + 1)] += 1
            metrics.settlement_payout += self.settings.stakes[active.failures]
            metrics.net_profit = (
                metrics.sell_proceeds
                + metrics.settlement_payout
                - metrics.total_making_amount
                - metrics.fees
                - metrics.slippage
            )
            self.state.active_round = None
            return "reversal_success"

        active.failures += 1
        if active.failures >= len(self.settings.stakes):
            metrics.forced_exit_rounds += 1
            self.state.active_round = None
            return "forced_exit_after_four_failures"
        active.execution_phase = "idle"
        active.split_transaction_hash = None
        active.exit_sold_shares = Decimal("0")
        active.exit_sell_proceeds = Decimal("0")
        return "trend_continued"

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
            )
            if isinstance(active_payload, dict)
            else None
        )
        daily = {
            str(key): DailyMetrics(
                settled_windows=int(value.get("settled_windows", 0)),
                triggered_rounds=int(value.get("triggered_rounds", 0)),
                successful_rounds=int(value.get("successful_rounds", 0)),
                forced_exit_rounds=int(value.get("forced_exit_rounds", 0)),
                stage_successes={
                    str(stage): int(count)
                    for stage, count in (value.get("stage_successes") or {}).items()
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
            last_volatility_pause_slug=payload.get("last_volatility_pause_slug"),
            reported_days=[str(value) for value in payload.get("reported_days", [])],
            daily=daily,
        )
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


def format_daily_report(report_day: date, metrics: DailyMetrics) -> str:
    stages = " / ".join(
        f"第{stage}次 {metrics.stage_successes.get(str(stage), 0)}"
        for stage in range(1, 5)
    )
    return "\n".join(
        [
            f"📊 BTC 5分钟反转策略 V1.1 日报 {report_day.isoformat()}",
            f"总结算窗口: {metrics.settled_windows}",
            f"触发轮数: {metrics.triggered_rounds}",
            f"成功轮数: {metrics.successful_rounds}",
            f"四次失败退出: {metrics.forced_exit_rounds}",
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


def _are_immediate_predecessors(recent_slugs: list[str], window_slug: str) -> bool:
    if len(recent_slugs) < 2:
        return False
    try:
        current_epoch = int(window_slug.rpartition("-")[2])
        first_epoch = int(recent_slugs[-2].rpartition("-")[2])
        second_epoch = int(recent_slugs[-1].rpartition("-")[2])
    except ValueError:
        return False
    return first_epoch == current_epoch - 600 and second_epoch == current_epoch - 300
