from __future__ import annotations

import json
import math
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


def _clamp(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return min(upper, max(lower, value))


def _logit(probability: Decimal) -> Decimal:
    bounded = _clamp(probability, Decimal("0.0001"), Decimal("0.9999"))
    return Decimal(str(math.log(float(bounded / (ONE - bounded)))))


def _logistic(value: Decimal) -> Decimal:
    return Decimal(str(ONE / (ONE + Decimal(str(math.exp(-float(value)))))))


def calibrate_probability(
    probability: Decimal,
    shrinkage: Decimal,
) -> Decimal:
    """Regularize an overconfident probability toward an even outcome.

    The transform is symmetric, monotonic, and preserves 0.50.  A shrinkage
    of one leaves the model unchanged while zero removes all directional
    confidence.
    """
    if not ZERO <= probability <= ONE:
        raise ValueError("probability must be within [0, 1]")
    if not ZERO <= shrinkage <= ONE:
        raise ValueError("probability shrinkage must be within [0, 1]")
    return Decimal("0.5") + shrinkage * (probability - Decimal("0.5"))


def _fee_per_share(price: Decimal, fee_rate: Decimal) -> Decimal:
    return fee_rate * price * (ONE - price)


def _book_timestamp_seconds(value: str) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.timestamp()
    if numeric > 10_000_000_000:
        numeric /= 1000.0
    return numeric


def _top_depth_imbalance(book: OrderBookSnapshot, levels: int = 3) -> Decimal:
    bids = sorted(book.bids, key=lambda level: level.price, reverse=True)[:levels]
    asks = sorted(book.asks, key=lambda level: level.price)[:levels]
    bid_depth = sum((level.size for level in bids), ZERO)
    ask_depth = sum((level.size for level in asks), ZERO)
    total = bid_depth + ask_depth
    return (bid_depth - ask_depth) / total if total > 0 else ZERO


def _vwap_for_buy(
    book: OrderBookSnapshot,
    quantity: Decimal,
) -> tuple[Decimal, Decimal] | None:
    if quantity <= 0:
        return None
    remaining = quantity
    cost = ZERO
    worst_price = ZERO
    for level in sorted(book.asks, key=lambda item: item.price):
        if level.price <= 0 or level.size <= 0:
            continue
        taken = min(remaining, level.size)
        cost += taken * level.price
        worst_price = max(worst_price, level.price)
        remaining -= taken
        if remaining <= 0:
            return cost / quantity, worst_price
    return None


def _momentum_bps(
    prices: list[Decimal],
    sample_times: list[float],
    horizon_seconds: float,
) -> Decimal | None:
    if len(prices) < 2 or len(prices) != len(sample_times) or prices[-1] <= 0:
        return None
    target = sample_times[-1] - horizon_seconds
    index = 0
    for candidate, observed_at in enumerate(sample_times[:-1]):
        if observed_at <= target:
            index = candidate
        else:
            break
    previous = prices[index]
    elapsed = sample_times[-1] - sample_times[index]
    if previous <= 0 or elapsed <= 0:
        return None
    raw_bps = Decimal(str(math.log(float(prices[-1] / previous)))) * Decimal("10000")
    return raw_bps / Decimal(str(elapsed))


@dataclass(frozen=True)
class FastDirectionalHedgeSettings:
    strategy_id: str = "fast_directional_hedge"
    strategy_version: str = "1.5.0"
    model_version: str = "book_lead_v1"
    feature_version: str = "microstructure_v2_instant_spot"
    parameter_version: str = "fdh_v1_4_lead_guard"
    probability_shrinkage: Decimal = Decimal("0.35")
    minimum_directional_probability: Decimal = Decimal("0.58")
    loss_reducing_hedge_min_probability: Decimal = Decimal("0.52")
    max_hedge_pair_cost_overrun_per_share: Decimal = Decimal("0.05")
    min_hedge_worst_loss_reduction: Decimal = Decimal("0.25")
    minimum_market_confirmation_probability: Decimal = Decimal("0.50")
    max_model_market_probability_gap: Decimal = Decimal("0.15")
    orderbook_first: bool = False
    orderbook_leader_probability: Decimal = Decimal("0.58")
    orderbook_confirmation_seconds: Decimal = Decimal("5")
    orderbook_max_entry_price: Decimal = Decimal("0.60")
    orderbook_max_spread: Decimal = Decimal("0.05")
    orderbook_min_imbalance: Decimal = Decimal("0.10")
    orderbook_min_flow_imbalance: Decimal = Decimal("0.10")
    adverse_average_tolerance: Decimal = Decimal("0.01")
    minimum_net_edge: Decimal = Decimal("0.02")
    entry_edge: Decimal = Decimal("0.04")
    strong_edge: Decimal = Decimal("0.07")
    hold_edge: Decimal = Decimal("0.02")
    hedge_entry_edge: Decimal = Decimal("0.04")
    safety_margin: Decimal = Decimal("0.005")
    fee_rate: Decimal = Decimal("0.07")
    base_order_size: Decimal = Decimal("2")
    strong_order_size: Decimal = Decimal("3")
    strong_size_min_probability: Decimal = Decimal("0.62")
    strong_size_min_edge: Decimal = Decimal("0.06")
    strong_size_min_seconds: Decimal = Decimal("120")
    max_directional_exposure: Decimal = Decimal("8")
    max_exposure_per_side: Decimal = Decimal("12")
    max_directional_adds_per_side: int = 1
    max_add_count: int = 6
    max_order_count_per_window: int = 8
    max_cost_basis: Decimal = Decimal("10")
    max_loss_per_market: Decimal = Decimal("6")
    max_position_value: Decimal = Decimal("10")
    max_unhedged_exposure: Decimal = Decimal("8")
    initial_target_minor_major_ratio: Decimal = Decimal("0.36")
    max_book_age_seconds: Decimal = Decimal("0.50")
    normal_max_slippage: Decimal = Decimal("0.02")
    fast_max_slippage: Decimal = Decimal("0.01")
    entry_start_seconds: Decimal = Decimal("295")
    entry_cutoff_seconds: Decimal = Decimal("75")
    momentum_beta: Decimal = Decimal("0.12")
    acceleration_beta: Decimal = Decimal("0.08")
    obi_beta: Decimal = Decimal("0.20")
    ofi_beta: Decimal = Decimal("0.12")

    def __post_init__(self) -> None:
        edges = (
            self.minimum_net_edge,
            self.entry_edge,
            self.strong_edge,
            self.hold_edge,
            self.hedge_entry_edge,
        )
        if any(edge < 0 or edge >= 1 for edge in edges):
            raise ValueError("edge thresholds must be within [0, 1)")
        if self.strong_edge <= self.entry_edge:
            raise ValueError("strong_edge must exceed entry_edge")
        if not ZERO <= self.probability_shrinkage <= ONE:
            raise ValueError("probability shrinkage must be within [0, 1]")
        if not Decimal("0.5") <= self.minimum_directional_probability <= ONE:
            raise ValueError("minimum directional probability must be within [0.5, 1]")
        if not Decimal("0.5") <= self.loss_reducing_hedge_min_probability <= self.minimum_directional_probability:
            raise ValueError("loss-reducing hedge probability must be between 0.5 and the directional minimum")
        if min(
            self.max_hedge_pair_cost_overrun_per_share,
            self.min_hedge_worst_loss_reduction,
            self.adverse_average_tolerance,
        ) < 0:
            raise ValueError("loss-reducing hedge limits cannot be negative")
        if not Decimal("0.5") <= self.minimum_market_confirmation_probability <= ONE:
            raise ValueError("market confirmation probability must be within [0.5, 1]")
        if not ZERO <= self.max_model_market_probability_gap <= ONE:
            raise ValueError("model/market probability gap must be within [0, 1]")
        if not Decimal("0.5") < self.orderbook_leader_probability < ONE:
            raise ValueError("order-book leader probability must be within (0.5, 1)")
        if not Decimal("0.5") < self.orderbook_max_entry_price < ONE:
            raise ValueError("order-book maximum entry price must be within (0.5, 1)")
        if min(
            self.orderbook_confirmation_seconds,
            self.orderbook_max_spread,
            self.orderbook_min_imbalance,
            self.orderbook_min_flow_imbalance,
        ) < 0:
            raise ValueError("order-book signal thresholds cannot be negative")
        if not self.minimum_directional_probability <= self.strong_size_min_probability <= ONE:
            raise ValueError("strong-size probability must be at least the directional minimum")
        if not ZERO <= self.strong_size_min_edge < ONE:
            raise ValueError("strong-size edge must be within [0, 1)")
        if not ZERO < self.initial_target_minor_major_ratio <= ONE:
            raise ValueError("initial pair ratio must be within (0, 1]")
        if min(
            self.safety_margin,
            self.fee_rate,
            self.max_book_age_seconds,
            self.normal_max_slippage,
            self.fast_max_slippage,
            self.entry_cutoff_seconds,
        ) < 0:
            raise ValueError("cost, age and timing settings cannot be negative")
        if self.entry_start_seconds <= self.entry_cutoff_seconds:
            raise ValueError("entry start must exceed entry cutoff")
        if (
            self.max_directional_adds_per_side < 1
            or self.max_add_count < 1
            or self.max_order_count_per_window < 1
        ):
            raise ValueError("order limits must be positive")
        if min(
            self.base_order_size,
            self.strong_order_size,
            self.strong_size_min_seconds,
            self.max_directional_exposure,
            self.max_exposure_per_side,
            self.max_cost_basis,
            self.max_loss_per_market,
            self.max_position_value,
            self.max_unhedged_exposure,
        ) <= 0:
            raise ValueError("inventory and risk limits must be positive")
        if self.strong_order_size < self.base_order_size:
            raise ValueError("strong order size must be at least the base order size")


@dataclass
class InventoryLot:
    side: str
    quantity: Decimal
    price: Decimal
    fee_per_share: Decimal
    filled_at: str
    fair_at_fill: Decimal | None = None

    @property
    def unit_cost(self) -> Decimal:
        return self.price + self.fee_per_share


@dataclass(frozen=True)
class PairMetrics:
    paired_qty: Decimal
    arbitrage_pair_qty: Decimal
    hedge_pair_qty: Decimal
    locked_arbitrage_profit: Decimal
    hedge_pair_cost_overrun: Decimal
    directional_side: str | None
    directional_qty: Decimal
    minor_major_ratio: Decimal


@dataclass(frozen=True)
class FairProbabilitySnapshot:
    raw_fair_up_probability: Decimal
    fair_up_probability: Decimal
    fair_down_probability: Decimal
    model_confidence: Decimal
    momentum_500ms: Decimal | None
    momentum_3s: Decimal | None
    acceleration: Decimal | None
    order_book_imbalance: Decimal
    order_flow_imbalance: Decimal
    volatility: Decimal
    distance_to_strike: Decimal
    time_to_expiry_sec: Decimal
    observed_at: str


@dataclass(frozen=True)
class HedgeDecision:
    side: str
    quantity: Decimal
    limit_price: Decimal
    executable_price: Decimal
    fair_probability: Decimal
    net_edge: Decimal
    target_exposure: Decimal
    required_delta: Decimal
    market_regime: str
    snapshot: FairProbabilitySnapshot
    pair_metrics: PairMetrics
    reason: str


@dataclass
class FastDirectionalHedgeState:
    market_slug: str | None = None
    lots: list[InventoryLot] = field(default_factory=list)
    add_count: int = 0
    order_count: int = 0
    previous_microstructure: dict[str, Decimal] = field(default_factory=dict)


class FastDirectionalHedgeEngine:
    """Interpretable fair-value, target-inventory and pair-conversion engine.

    It deliberately emits only BUY deltas. Opposite-side BUYs convert existing
    directional lots into pairs; it never reports a hedge pair as arbitrage.
    """

    def __init__(
        self,
        settings: FastDirectionalHedgeSettings | None = None,
        state_path: Path | None = None,
        recorder_path: Path | None = None,
    ) -> None:
        self.settings = settings or FastDirectionalHedgeSettings()
        self.state_path = state_path
        self.recorder_path = recorder_path
        self.state = self._load_state() if state_path else FastDirectionalHedgeState()
        self._book_leader_side: str | None = None
        self._book_leader_since: float | None = None
        self._book_leader_samples = 0

    def begin_market(self, slug: str) -> None:
        if self.state.market_slug == slug:
            return
        self.state = FastDirectionalHedgeState(market_slug=slug)
        self._book_leader_side = None
        self._book_leader_since = None
        self._book_leader_samples = 0
        self._save_state()

    def quantities(self) -> tuple[Decimal, Decimal]:
        up = sum((lot.quantity for lot in self.state.lots if lot.side == "UP"), ZERO)
        down = sum((lot.quantity for lot in self.state.lots if lot.side == "DOWN"), ZERO)
        return up, down

    def total_cost_basis(self) -> Decimal:
        return sum((lot.quantity * lot.unit_cost for lot in self.state.lots), ZERO)

    def pair_metrics(self) -> PairMetrics:
        up_lots = [[lot.quantity, lot.unit_cost] for lot in self.state.lots if lot.side == "UP"]
        down_lots = [[lot.quantity, lot.unit_cost] for lot in self.state.lots if lot.side == "DOWN"]
        # WorstInventoryFirst: expensive inventory is neutralized first.
        up_lots.sort(key=lambda item: item[1], reverse=True)
        down_lots.sort(key=lambda item: item[1], reverse=True)
        up_qty = sum((item[0] for item in up_lots), ZERO)
        down_qty = sum((item[0] for item in down_lots), ZERO)
        paired = ZERO
        arbitrage_qty = ZERO
        hedge_qty = ZERO
        arbitrage_profit = ZERO
        hedge_overrun = ZERO
        up_index = down_index = 0
        while up_index < len(up_lots) and down_index < len(down_lots):
            quantity = min(up_lots[up_index][0], down_lots[down_index][0])
            pair_cost = up_lots[up_index][1] + down_lots[down_index][1]
            paired += quantity
            if pair_cost < ONE:
                arbitrage_qty += quantity
                arbitrage_profit += quantity * (ONE - pair_cost)
            else:
                hedge_qty += quantity
                hedge_overrun += quantity * (pair_cost - ONE)
            up_lots[up_index][0] -= quantity
            down_lots[down_index][0] -= quantity
            if up_lots[up_index][0] <= 0:
                up_index += 1
            if down_lots[down_index][0] <= 0:
                down_index += 1
        major = max(up_qty, down_qty)
        minor = min(up_qty, down_qty)
        return PairMetrics(
            paired_qty=paired,
            arbitrage_pair_qty=arbitrage_qty,
            hedge_pair_qty=hedge_qty,
            locked_arbitrage_profit=arbitrage_profit,
            hedge_pair_cost_overrun=hedge_overrun,
            directional_side="UP" if up_qty > down_qty else "DOWN" if down_qty > up_qty else None,
            directional_qty=abs(up_qty - down_qty),
            minor_major_ratio=minor / major if major > 0 else ZERO,
        )

    def record_fill(
        self,
        slug: str,
        side: str,
        quantity: Decimal,
        cost: Decimal,
        fair_probability: Decimal | None = None,
        filled_at: str | None = None,
    ) -> None:
        self.begin_market(slug)
        if side not in {"UP", "DOWN"} or quantity <= 0 or cost <= 0:
            raise ValueError("fill must have a valid side, quantity and cost")
        price = cost / quantity
        self.state.lots.append(
            InventoryLot(
                side=side,
                quantity=quantity,
                price=price,
                fee_per_share=_fee_per_share(price, self.settings.fee_rate),
                filled_at=filled_at or datetime.now(timezone.utc).isoformat(),
                fair_at_fill=fair_probability,
            )
        )
        self.state.add_count += 1
        self.state.order_count += 1
        self._save_state()
        self._record(
            "fill",
            {
                "slug": slug,
                "side": side,
                "quantity": quantity,
                "cost": cost,
                "fair_at_fill": fair_probability,
                "pair_metrics": self.pair_metrics(),
            },
        )

    def evaluate(
        self,
        *,
        slug: str,
        strike: Decimal,
        spot: Decimal,
        seconds_to_expiry: Decimal,
        sigma_per_sqrt_second: Decimal,
        base_probability_up: Decimal,
        spot_prices: list[Decimal],
        sample_times: list[float],
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
        observed_at: float | None = None,
    ) -> HedgeDecision | None:
        self.begin_market(slug)
        now = time.time() if observed_at is None else observed_at
        settings = self.settings
        pre_evaluation_metrics = self.pair_metrics()
        has_directional_position = (
            pre_evaluation_metrics.directional_side is not None
            and pre_evaluation_metrics.directional_qty > 0
        )
        invalid_market_inputs = strike <= 0 or spot <= 0 or sigma_per_sqrt_second <= 0
        outside_opening_window = (
            seconds_to_expiry > settings.entry_start_seconds
            or seconds_to_expiry < settings.entry_cutoff_seconds
        )
        if (
            invalid_market_inputs
            or seconds_to_expiry <= 0
            or (outside_opening_window and not has_directional_position)
        ):
            return None
        # Opening limits must never suppress the one order that removes an
        # already-open directional exposure.  A held order-book position is
        # monitored until expiry even after new entries have closed.
        if self.state.add_count >= settings.max_add_count and not has_directional_position:
            return None
        if self.state.order_count >= settings.max_order_count_per_window and not has_directional_position:
            return None
        for book in (up_book, down_book):
            timestamp = _book_timestamp_seconds(book.timestamp)
            if timestamp is None or Decimal(str(max(0.0, now - timestamp))) > settings.max_book_age_seconds:
                self._record("skip", {"slug": slug, "reason": "stale_or_unknown_book"})
                return None

        momentum_short = _momentum_bps(spot_prices, sample_times, 0.5)
        momentum_long = _momentum_bps(spot_prices, sample_times, 3.0)
        acceleration = (
            momentum_short - momentum_long
            if momentum_short is not None and momentum_long is not None
            else None
        )
        up_obi = _top_depth_imbalance(up_book)
        down_obi = _top_depth_imbalance(down_book)
        obi = (up_obi - down_obi) / Decimal("2")
        ofi = self._inferred_ofi(up_book, down_book)
        logit_adjustment = ZERO
        if momentum_short is not None:
            logit_adjustment += settings.momentum_beta * _clamp(
                momentum_short / Decimal("2"), Decimal("-2"), Decimal("2")
            )
        if acceleration is not None:
            logit_adjustment += settings.acceleration_beta * _clamp(
                acceleration / Decimal("2"), Decimal("-2"), Decimal("2")
            )
        logit_adjustment += settings.obi_beta * obi + settings.ofi_beta * ofi
        raw_fair_up = _clamp(
            _logistic(_logit(base_probability_up) + logit_adjustment),
            Decimal("0.001"),
            Decimal("0.999"),
        )
        fair_up = calibrate_probability(
            raw_fair_up,
            settings.probability_shrinkage,
        )
        confidence_features = 3 + int(momentum_short is not None) + int(acceleration is not None)
        confidence = Decimal(str(confidence_features)) / Decimal("5")
        snapshot = FairProbabilitySnapshot(
            raw_fair_up_probability=raw_fair_up,
            fair_up_probability=fair_up,
            fair_down_probability=ONE - fair_up,
            model_confidence=confidence,
            momentum_500ms=momentum_short,
            momentum_3s=momentum_long,
            acceleration=acceleration,
            order_book_imbalance=obi,
            order_flow_imbalance=ofi,
            volatility=sigma_per_sqrt_second,
            distance_to_strike=spot - strike,
            time_to_expiry_sec=seconds_to_expiry,
            observed_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
        )
        market_regime = self._market_regime(momentum_short, sigma_per_sqrt_second)
        slippage = (
            settings.fast_max_slippage
            if market_regime in {"FAST", "SHOCK"}
            else settings.normal_max_slippage
        )
        candidates: list[tuple[Decimal, str, OrderBookSnapshot, Decimal, Decimal, Decimal]] = []
        for side, book, raw_probability, calibrated_probability in (
            ("UP", up_book, raw_fair_up, fair_up),
            ("DOWN", down_book, ONE - raw_fair_up, ONE - fair_up),
        ):
            # Calibration is a confidence haircut, never a source of new edge.
            # Symmetric shrinkage raises the underdog probability; using that
            # raised value alone caused the strategy to buy directions that the
            # raw model explicitly considered negative-EV.  The lower bound
            # preserves calibration on the favored side without manufacturing
            # underdog trades.
            probability = min(raw_probability, calibrated_probability)
            execution = _vwap_for_buy(book, settings.base_order_size)
            if execution is None:
                continue
            executable, worst = execution
            net_edge = (
                probability
                - executable
                - _fee_per_share(executable, settings.fee_rate)
                - slippage
                - settings.safety_margin
            )
            candidates.append((net_edge, side, book, probability, executable, worst))
        if not candidates:
            self._record("decision", {"slug": slug, "snapshot": snapshot, "action": "HOLD_NO_DEPTH"})
            return None
        executable_by_side = {item[1]: item[4] for item in candidates}
        executable_sum = sum(executable_by_side.values(), ZERO)
        if executable_sum <= 0 or set(executable_by_side) != {"UP", "DOWN"}:
            self._record(
                "decision",
                {"slug": slug, "snapshot": snapshot, "action": "HOLD_MARKET_PRIOR"},
            )
            return None
        market_probability_by_side = {
            side_name: side_price / executable_sum
            for side_name, side_price in executable_by_side.items()
        }
        current_metrics = self.pair_metrics()
        stop_loss_pair = False
        if current_metrics.directional_side is not None and current_metrics.directional_qty > 0:
            held_side = current_metrics.directional_side
            held_candidate = next(item for item in candidates if item[1] == held_side)
            held_market_probability = market_probability_by_side[held_side]
            held_model_market_gap = abs(held_candidate[3] - held_market_probability)
            book_first_hold = settings.orderbook_first and held_market_probability >= Decimal("0.50")
            model_edge_hold = (
                not settings.orderbook_first
                and held_candidate[0] >= settings.hold_edge
                and held_market_probability >= settings.minimum_market_confirmation_probability
                and held_model_market_gap <= settings.max_model_market_probability_gap
            )
            if book_first_hold or model_edge_hold:
                self._record(
                    "decision",
                    {
                        "slug": slug,
                        "snapshot": snapshot,
                        "held_side": held_side,
                        "held_net_edge": held_candidate[0],
                        "held_market_probability": held_market_probability,
                        "held_model_market_gap": held_model_market_gap,
                        "hold_edge": settings.hold_edge,
                        "action": "HOLD_BOOK_LEAD" if settings.orderbook_first else "HOLD_POSITION_EDGE",
                    },
                )
                return None
            exit_side = "DOWN" if held_side == "UP" else "UP"
            net_edge, side, book, probability, executable, worst_price = next(
                item for item in candidates if item[1] == exit_side
            )
            stop_loss_pair = True
        else:
            if settings.orderbook_first:
                side = max(market_probability_by_side, key=market_probability_by_side.get)
                net_edge, _, book, probability, executable, worst_price = next(
                    item for item in candidates if item[1] == side
                )
                leader_probability = market_probability_by_side[side]
                if leader_probability < settings.orderbook_leader_probability:
                    self._reset_book_leader()
                    self._record("decision", {"slug": slug, "snapshot": snapshot, "action": "HOLD_BOOK_NO_LEADER"})
                    return None
                self._observe_book_leader(side, now)
                quote = book.quote
                side_imbalance = obi if side == "UP" else -obi
                side_flow = ofi if side == "UP" else -ofi
                confirmed_for = now - (self._book_leader_since or now)
                if (
                    self._book_leader_samples < 2
                    or confirmed_for < float(settings.orderbook_confirmation_seconds)
                    or executable > settings.orderbook_max_entry_price
                    or quote.bid is None
                    or executable - quote.bid > settings.orderbook_max_spread
                    or side_imbalance < settings.orderbook_min_imbalance
                    or side_flow < settings.orderbook_min_flow_imbalance
                ):
                    self._record(
                        "decision",
                        {
                            "slug": slug,
                            "snapshot": snapshot,
                            "selected_side": side,
                            "market_probability": leader_probability,
                            "leader_confirmed_seconds": confirmed_for,
                            "side_imbalance": side_imbalance,
                            "side_flow_imbalance": side_flow,
                            "action": "HOLD_BOOK_SETUP",
                        },
                    )
                    return None
            else:
                net_edge, side, book, probability, executable, worst_price = max(
                    candidates, key=lambda item: item[0]
                )
        edge_by_side = {item[1]: item[0] for item in candidates}
        up_qty, down_qty = self.quantities()
        side_qty = up_qty if side == "UP" else down_qty
        other_qty = down_qty if side == "UP" else up_qty
        signed_exposure = (up_qty - down_qty) if side == "UP" else (down_qty - up_qty)
        estimated_unit_cost = executable + _fee_per_share(executable, settings.fee_rate)
        is_pair_conversion = signed_exposure < 0
        market_probability = market_probability_by_side[side]
        if not settings.orderbook_first and not is_pair_conversion and (
            market_probability < settings.minimum_market_confirmation_probability
            or abs(probability - market_probability)
            > settings.max_model_market_probability_gap
        ):
            self._record(
                "decision",
                {
                    "slug": slug,
                    "snapshot": snapshot,
                    "selected_side": side,
                    "selected_probability": probability,
                    "market_probability": market_probability,
                    "minimum_market_confirmation_probability": (
                        settings.minimum_market_confirmation_probability
                    ),
                    "max_model_market_probability_gap": (
                        settings.max_model_market_probability_gap
                    ),
                    "action": "HOLD_MARKET_LEAD_DISAGREEMENT",
                },
            )
            return None
        same_side_lots = [lot for lot in self.state.lots if lot.side == side]
        if (
            not is_pair_conversion
            and len(same_side_lots) >= settings.max_directional_adds_per_side
        ):
            self._record(
                "decision",
                {
                    "slug": slug,
                    "snapshot": snapshot,
                    "selected_side": side,
                    "directional_adds_for_side": len(same_side_lots),
                    "action": "HOLD_DIRECTIONAL_ADD_LIMIT",
                },
            )
            return None
        if (
            signed_exposure > 0
            and same_side_lots
            and executable + settings.adverse_average_tolerance
            < same_side_lots[-1].price
        ):
            self._record(
                "decision",
                {
                    "slug": slug,
                    "snapshot": snapshot,
                    "selected_side": side,
                    "executable_price": executable,
                    "last_fill_price": same_side_lots[-1].price,
                    "action": "HOLD_ADVERSE_AVERAGING",
                },
            )
            return None
        loss_reducing_hedge = False
        if not settings.orderbook_first and probability < settings.minimum_directional_probability and not stop_loss_pair:
            next_pair_is_arbitrage = is_pair_conversion and self._next_pair_is_arbitrage(
                side=side, estimated_unit_cost=estimated_unit_cost
            )
            loss_reducing_hedge = (
                is_pair_conversion
                and not next_pair_is_arbitrage
                and side_qty == 0
                and probability >= settings.loss_reducing_hedge_min_probability
                and self._next_pair_cost_overrun(
                    side=side,
                    estimated_unit_cost=estimated_unit_cost,
                ) <= settings.max_hedge_pair_cost_overrun_per_share
            )
            if not next_pair_is_arbitrage and not loss_reducing_hedge:
                self._record(
                    "decision",
                    {
                        "slug": slug,
                        "snapshot": snapshot,
                        "selected_side": side,
                        "selected_probability": probability,
                        "minimum_directional_probability": settings.minimum_directional_probability,
                        "action": "HOLD_PROBABILITY",
                    },
                )
                return None
        minimum_to_add = settings.entry_edge
        if signed_exposure < 0:
            minimum_to_add = max(minimum_to_add, settings.hedge_entry_edge)
        if not settings.orderbook_first and net_edge < minimum_to_add and not stop_loss_pair:
            self._record(
                "decision",
                {
                    "slug": slug,
                    "snapshot": snapshot,
                    "net_edge_up": next((item[0] for item in candidates if item[1] == "UP"), None),
                    "net_edge_down": next((item[0] for item in candidates if item[1] == "DOWN"), None),
                    "action": "HOLD_EDGE",
                },
            )
            return None

        edge_fraction = _clamp(
            (net_edge - settings.entry_edge) / (settings.strong_edge - settings.entry_edge),
            ZERO,
            ONE,
        )
        target_fraction = Decimal("0.25") + Decimal("0.75") * edge_fraction
        volatility_floor = Decimal("0.00005")
        volatility_multiplier = _clamp(
            volatility_floor / sigma_per_sqrt_second,
            Decimal("0.35"),
            ONE,
        )
        target = settings.max_directional_exposure * target_fraction * volatility_multiplier
        target_ratio: Decimal | None = None
        if settings.orderbook_first and not stop_loss_pair:
            required_delta = max(
                settings.base_order_size,
                ONE / max(executable, Decimal("0.01")),
            )
        elif other_qty > side_qty:
            # A new opposite signal first moves inventory toward a dynamic Pair
            # ratio. Only after the book is balanced can later evaluations build
            # fresh directional exposure on the new side.
            if stop_loss_pair or net_edge >= settings.strong_edge:
                target_ratio = ONE
            else:
                ratio_progress = _clamp(
                    (net_edge - settings.entry_edge)
                    / (settings.strong_edge - settings.entry_edge),
                    ZERO,
                    ONE,
                )
                target_ratio = _clamp(
                    settings.initial_target_minor_major_ratio
                    + Decimal("0.34") * ratio_progress,
                    Decimal("0.15"),
                    ONE,
                )
            required_delta = max(ZERO, other_qty * target_ratio - side_qty)
        else:
            required_delta = max(ZERO, target - signed_exposure)
        strong_size = (
            not settings.orderbook_first
            and probability >= settings.strong_size_min_probability
            and net_edge >= settings.strong_size_min_edge
            and seconds_to_expiry >= settings.strong_size_min_seconds
            and signed_exposure >= 0
        )
        planned_order_size = (
            required_delta
            if stop_loss_pair
            else settings.strong_order_size
            if strong_size
            else settings.base_order_size
        )
        minimum_marketable_quantity = ONE / max(executable, Decimal("0.01"))
        desired_order_quantity = max(planned_order_size, minimum_marketable_quantity)
        allowed = self._risk_limited_quantity(
            side=side,
            desired=min(required_delta, desired_order_quantity),
            estimated_unit_cost=estimated_unit_cost,
            risk_reducing=stop_loss_pair or loss_reducing_hedge,
        )
        quantity = min(required_delta, allowed)
        quantity = quantity.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if loss_reducing_hedge or stop_loss_pair:
            worst_loss_reduction = quantity * max(ZERO, ONE - estimated_unit_cost)
            required_reduction = (
                ZERO if stop_loss_pair else settings.min_hedge_worst_loss_reduction
            )
            if worst_loss_reduction <= required_reduction:
                self._record(
                    "decision",
                    {
                        "slug": slug,
                        "snapshot": snapshot,
                        "worst_loss_reduction": worst_loss_reduction,
                        "required_loss_reduction": required_reduction,
                        "action": "HOLD_HEDGE_REDUCTION",
                    },
                )
                return None
        if quantity < minimum_marketable_quantity:
            self._record("decision", {"slug": slug, "snapshot": snapshot, "action": "HOLD_RISK_OR_SIZE"})
            return None
        execution = _vwap_for_buy(book, quantity)
        if execution is None:
            return None
        executable, worst_price = execution
        net_edge = (
            probability
            - executable
            - _fee_per_share(executable, settings.fee_rate)
            - slippage
            - settings.safety_margin
        )
        if not settings.orderbook_first and net_edge < settings.minimum_net_edge and not stop_loss_pair:
            return None
        edge_preserving_cap = (
            probability
            - _fee_per_share(executable, settings.fee_rate)
            - settings.safety_margin
            - settings.minimum_net_edge
        )
        if settings.orderbook_first and not stop_loss_pair:
            limit_price = min(worst_price + slippage, settings.orderbook_max_entry_price)
        else:
            limit_price = min(
                worst_price + slippage,
                Decimal("0.99") if stop_loss_pair else edge_preserving_cap,
                Decimal("0.99"),
            )
        tick = Decimal("0.01")
        limit_price = (limit_price / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        if limit_price < worst_price:
            return None
        metrics = self.pair_metrics()
        reason = (
            f"fast_directional_hedge_v1.5.0 fair={probability:.4f} "
            f"raw_fair_up={raw_fair_up:.4f} "
            f"calibration={settings.probability_shrinkage:.2f} "
            f"exec_vwap={executable:.4f} net_edge={net_edge:.4f} "
            f"target={target:.4f} required_delta={required_delta:.4f} "
            f"order_tier={'book_stop' if settings.orderbook_first and stop_loss_pair else 'edge_stop' if stop_loss_pair else 'book_entry' if settings.orderbook_first else 'risk_hedge' if loss_reducing_hedge else 'strong' if strong_size else 'base'} "
            f"target_ratio={target_ratio if target_ratio is not None else 'directional'} "
            f"market_probability={market_probability:.4f} "
            f"opposite_edge={edge_by_side.get('DOWN' if side == 'UP' else 'UP', ZERO):.4f} "
            f"obi={obi:.4f} ofi={ofi:.4f} regime={market_regime}"
        )
        decision = HedgeDecision(
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            executable_price=executable,
            fair_probability=probability,
            net_edge=net_edge,
            target_exposure=target,
            required_delta=required_delta,
            market_regime=market_regime,
            snapshot=snapshot,
            pair_metrics=metrics,
            reason=reason,
        )
        self._record("decision", {"slug": slug, "action": "BUY", "decision": decision})
        return decision

    def _reset_book_leader(self) -> None:
        self._book_leader_side = None
        self._book_leader_since = None
        self._book_leader_samples = 0

    def _observe_book_leader(self, side: str, observed_at: float) -> None:
        if side != self._book_leader_side:
            self._book_leader_side = side
            self._book_leader_since = observed_at
            self._book_leader_samples = 1
            return
        self._book_leader_samples += 1

    def _next_pair_is_arbitrage(
        self,
        *,
        side: str,
        estimated_unit_cost: Decimal,
    ) -> bool:
        """Allow a low-confidence opposite buy only when it locks profit.

        Pair accounting consumes expensive inventory first, so the next buy is
        safe only when it pairs profitably even with the most expensive lot on
        the other side.  This prevents a weak probability from being justified
        as a hedge while actually creating a pair whose total cost exceeds one.
        """
        opposite_side = "DOWN" if side == "UP" else "UP"
        opposite_costs = [
            lot.unit_cost for lot in self.state.lots if lot.side == opposite_side
        ]
        return bool(opposite_costs) and max(opposite_costs) + estimated_unit_cost < ONE

    def _next_pair_cost_overrun(
        self,
        *,
        side: str,
        estimated_unit_cost: Decimal,
    ) -> Decimal:
        opposite_side = "DOWN" if side == "UP" else "UP"
        opposite_costs = [
            lot.unit_cost for lot in self.state.lots if lot.side == opposite_side
        ]
        if not opposite_costs:
            return ONE
        return max(ZERO, max(opposite_costs) + estimated_unit_cost - ONE)

    def _risk_limited_quantity(
        self,
        *,
        side: str,
        desired: Decimal,
        estimated_unit_cost: Decimal,
        risk_reducing: bool = False,
    ) -> Decimal:
        settings = self.settings
        up_qty, down_qty = self.quantities()
        side_qty = up_qty if side == "UP" else down_qty
        other_qty = down_qty if side == "UP" else up_qty
        total_cost = self.total_cost_basis()
        caps = [
            desired,
            settings.max_exposure_per_side - side_qty,
            max(ZERO, other_qty + settings.max_unhedged_exposure - side_qty),
        ]
        if not risk_reducing:
            caps.extend(
                [
                    (settings.max_cost_basis - total_cost) / estimated_unit_cost,
                    (settings.max_position_value - total_cost) / estimated_unit_cost,
                ]
            )
        current_worst_loss = max(ZERO, total_cost - min(up_qty, down_qty))
        if side_qty >= other_qty and not risk_reducing:
            caps.append((settings.max_loss_per_market - current_worst_loss) / estimated_unit_cost)
        else:
            # Pairing a minor-side share returns one unit in either outcome and
            # changes worst loss by unit_cost - 1. Costs below one reduce risk.
            marginal_loss = max(ZERO, estimated_unit_cost - ONE)
            if marginal_loss > 0:
                caps.append((settings.max_loss_per_market - current_worst_loss) / marginal_loss)
        return max(ZERO, min(caps))

    def _market_regime(
        self,
        momentum_short: Decimal | None,
        sigma_per_sqrt_second: Decimal,
    ) -> str:
        if sigma_per_sqrt_second >= Decimal("0.00020"):
            return "SHOCK"
        if sigma_per_sqrt_second >= Decimal("0.00010") or (
            momentum_short is not None and abs(momentum_short) >= Decimal("2")
        ):
            return "FAST"
        return "NORMAL"

    def _inferred_ofi(
        self,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
    ) -> Decimal:
        current: dict[str, Decimal] = {}
        for prefix, book in (("up", up_book), ("down", down_book)):
            quote = book.quote
            current[f"{prefix}_bid"] = quote.bid or ZERO
            current[f"{prefix}_ask"] = quote.ask or ONE
            current[f"{prefix}_bid_size"] = sum(
                (level.size for level in sorted(book.bids, key=lambda item: item.price, reverse=True)[:3]),
                ZERO,
            )
            current[f"{prefix}_ask_size"] = sum(
                (level.size for level in sorted(book.asks, key=lambda item: item.price)[:3]),
                ZERO,
            )
        previous = self.state.previous_microstructure
        self.state.previous_microstructure = current
        if not previous:
            self._save_state()
            return ZERO
        up_pressure = (
            current["up_bid_size"] - previous.get("up_bid_size", ZERO)
            - current["up_ask_size"] + previous.get("up_ask_size", ZERO)
        )
        down_pressure = (
            current["down_bid_size"] - previous.get("down_bid_size", ZERO)
            - current["down_ask_size"] + previous.get("down_ask_size", ZERO)
        )
        scale = sum(
            (
                current["up_bid_size"],
                current["up_ask_size"],
                current["down_bid_size"],
                current["down_ask_size"],
            ),
            ZERO,
        )
        self._save_state()
        return _clamp((up_pressure - down_pressure) / scale, -ONE, ONE) if scale > 0 else ZERO

    def _load_state(self) -> FastDirectionalHedgeState:
        assert self.state_path is not None
        if not self.state_path.exists():
            return FastDirectionalHedgeState()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return FastDirectionalHedgeState(
                market_slug=payload.get("market_slug"),
                lots=[
                    InventoryLot(
                        side=str(item["side"]),
                        quantity=Decimal(str(item["quantity"])),
                        price=Decimal(str(item["price"])),
                        fee_per_share=Decimal(str(item["fee_per_share"])),
                        filled_at=str(item["filled_at"]),
                        fair_at_fill=(
                            Decimal(str(item["fair_at_fill"]))
                            if item.get("fair_at_fill") is not None
                            else None
                        ),
                    )
                    for item in payload.get("lots", [])
                ],
                add_count=int(payload.get("add_count") or 0),
                order_count=int(payload.get("order_count") or 0),
                previous_microstructure={
                    str(key): Decimal(str(value))
                    for key, value in payload.get("previous_microstructure", {}).items()
                },
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return FastDirectionalHedgeState()

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._jsonable(self.state)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def _record(self, event: str, payload: dict[str, Any]) -> None:
        if self.recorder_path is None:
            return
        self.recorder_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy_id": self.settings.strategy_id,
            "strategy_version": self.settings.strategy_version,
            "model_version": self.settings.model_version,
            "feature_version": self.settings.feature_version,
            "parameter_version": self.settings.parameter_version,
            **payload,
        }
        with self.recorder_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._jsonable(record), ensure_ascii=False) + "\n")

    @classmethod
    def _jsonable(cls, value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if hasattr(value, "__dataclass_fields__"):
            return {key: cls._jsonable(item) for key, item in asdict(value).items()}
        if isinstance(value, dict):
            return {str(key): cls._jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._jsonable(item) for item in value]
        return value
