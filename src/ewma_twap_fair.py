from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR


@dataclass(frozen=True)
class EwmaTwapSettings:
    lambda_per_second: Decimal = Decimal("0.94")
    realized_window_seconds: Decimal = Decimal("60")
    ewma_weight: Decimal = Decimal("0.70")
    minimum_model_edge: Decimal = Decimal("0.015")
    half_spread_buffer: Decimal = Decimal("0.0025")
    slippage_buffer: Decimal = Decimal("0.0030")
    taker_fee_rate: Decimal = Decimal("0.07")
    kelly_fraction: Decimal = Decimal("0.25")
    kelly_bankroll: Decimal = Decimal("1000")
    max_notional: Decimal = Decimal("25")
    entry_start_seconds: Decimal = Decimal("300")
    entry_cutoff_seconds: Decimal = Decimal("75")

    def __post_init__(self) -> None:
        if not Decimal("0") < self.lambda_per_second < Decimal("1"):
            raise ValueError("lambda_per_second must be between zero and one")
        if not Decimal("0") <= self.ewma_weight <= Decimal("1"):
            raise ValueError("ewma_weight must be between zero and one")
        if self.realized_window_seconds <= 0:
            raise ValueError("realized_window_seconds must be positive")
        if self.entry_cutoff_seconds < 60:
            raise ValueError("entry_cutoff_seconds must be at least 60 for pre-TWAP entry")
        if self.entry_start_seconds <= self.entry_cutoff_seconds:
            raise ValueError("entry_start_seconds must exceed entry_cutoff_seconds")
        if min(
            self.minimum_model_edge,
            self.half_spread_buffer,
            self.slippage_buffer,
            self.taker_fee_rate,
            self.kelly_fraction,
            self.kelly_bankroll,
            self.max_notional,
        ) < 0:
            raise ValueError("EWMA TWAP settings cannot be negative")


@dataclass(frozen=True)
class EwmaTwapFairValue:
    probability_up: Decimal
    probability_down: Decimal
    sigma_per_sqrt_second: Decimal
    effective_variance_seconds: Decimal
    z_score: Decimal


@dataclass(frozen=True)
class EwmaTwapDecision:
    side: str
    probability: Decimal
    price: Decimal
    raw_edge: Decimal
    fee_per_share: Decimal
    net_model_edge: Decimal
    notional: Decimal
    shares: Decimal


def _timed_returns(
    samples: list[tuple[float, Decimal]],
) -> list[tuple[float, float, float]]:
    values: list[tuple[float, float, float]] = []
    for (left_time, left_price), (right_time, right_price) in zip(
        samples, samples[1:]
    ):
        elapsed = right_time - left_time
        if elapsed <= 0 or left_price <= 0 or right_price <= 0:
            continue
        values.append((right_time, math.log(float(right_price / left_price)), elapsed))
    return values


def time_aware_blended_sigma(
    samples: list[tuple[float, Decimal]],
    settings: EwmaTwapSettings,
    fallback_sigma: Decimal,
) -> Decimal | None:
    if len(samples) < 3:
        return None
    returns = _timed_returns(samples)
    if len(returns) < 2:
        return None

    ewma_variance_rate: float | None = None
    decay_base = float(settings.lambda_per_second)
    for _, value, elapsed in returns:
        observed_rate = value * value / elapsed
        decay = decay_base**elapsed
        ewma_variance_rate = (
            observed_rate
            if ewma_variance_rate is None
            else decay * ewma_variance_rate + (1.0 - decay) * observed_rate
        )
    if ewma_variance_rate is None:
        return None

    cutoff = samples[-1][0] - float(settings.realized_window_seconds)
    recent = [item for item in returns if item[0] >= cutoff]
    total_seconds = sum(elapsed for _, _, elapsed in recent)
    if len(recent) < 2 or total_seconds <= 0:
        realized_variance_rate = ewma_variance_rate
    else:
        drift_rate = sum(value for _, value, _ in recent) / total_seconds
        realized_variance_rate = sum(
            (value - drift_rate * elapsed) ** 2
            for _, value, elapsed in recent
        ) / total_seconds

    blended_variance_rate = (
        float(settings.ewma_weight) * ewma_variance_rate
        + (1.0 - float(settings.ewma_weight)) * realized_variance_rate
    )
    sigma = Decimal(str(math.sqrt(max(0.0, blended_variance_rate))))
    return max(sigma, fallback_sigma)


def ewma_twap_fair_value(
    samples: list[tuple[float, Decimal]],
    current_spot: Decimal,
    price_to_beat: Decimal,
    seconds_to_expiry: Decimal,
    settings: EwmaTwapSettings,
    fallback_sigma: Decimal,
) -> EwmaTwapFairValue | None:
    if current_spot <= 0 or price_to_beat <= 0:
        raise ValueError("current_spot and price_to_beat must be positive")
    sigma = time_aware_blended_sigma(samples, settings, fallback_sigma)
    if sigma is None:
        return None

    # Before the final 60-second averaging interval begins, integrated Brownian
    # noise gives Var(TWAP_60) = sigma^2 * (T - 2*60/3) = sigma^2 * (T - 40).
    effective_seconds = max(Decimal("0"), seconds_to_expiry - Decimal("40"))
    denominator = sigma * Decimal(str(math.sqrt(float(effective_seconds))))
    if denominator <= 0:
        probability_up = Decimal("1") if current_spot >= price_to_beat else Decimal("0")
        return EwmaTwapFairValue(
            probability_up=probability_up,
            probability_down=Decimal("1") - probability_up,
            sigma_per_sqrt_second=sigma,
            effective_variance_seconds=effective_seconds,
            z_score=Decimal("0"),
        )

    variance = sigma * sigma
    log_distance = Decimal(str(math.log(float(current_spot / price_to_beat))))
    z_score = (
        log_distance - Decimal("0.5") * variance * effective_seconds
    ) / denominator
    probability_up = Decimal(
        str(0.5 * (1.0 + math.erf(float(z_score) / math.sqrt(2.0))))
    )
    probability_up = min(Decimal("1"), max(Decimal("0"), probability_up))
    return EwmaTwapFairValue(
        probability_up=probability_up,
        probability_down=Decimal("1") - probability_up,
        sigma_per_sqrt_second=sigma,
        effective_variance_seconds=effective_seconds,
        z_score=z_score,
    )


def _fee_per_share(price: Decimal, fee_rate: Decimal) -> Decimal:
    return fee_rate * price * (Decimal("1") - price)


def _kelly_notional(
    probability: Decimal,
    price: Decimal,
    fee_per_share: Decimal,
    settings: EwmaTwapSettings,
) -> Decimal:
    loss_cost = price + fee_per_share
    win_profit = Decimal("1") - price - fee_per_share
    if loss_cost <= 0 or win_profit <= 0:
        return Decimal("0")
    odds = win_profit / loss_cost
    full_kelly = (
        odds * probability - (Decimal("1") - probability)
    ) / odds
    if full_kelly <= 0:
        return Decimal("0")
    return min(
        settings.max_notional,
        settings.kelly_bankroll * settings.kelly_fraction * full_kelly,
    )


def choose_ewma_twap_decision(
    fair: EwmaTwapFairValue,
    up_ask: Decimal | None,
    down_ask: Decimal | None,
    seconds_to_expiry: Decimal,
    settings: EwmaTwapSettings,
) -> EwmaTwapDecision | None:
    if not (
        settings.entry_cutoff_seconds
        < seconds_to_expiry
        <= settings.entry_start_seconds
    ):
        return None

    candidates: list[tuple[Decimal, str, Decimal, Decimal, Decimal, Decimal]] = []
    for side, probability, price in (
        ("UP", fair.probability_up, up_ask),
        ("DOWN", fair.probability_down, down_ask),
    ):
        if price is None or not Decimal("0") < price < Decimal("1"):
            continue
        fee = _fee_per_share(price, settings.taker_fee_rate)
        raw_edge = probability - price
        net_edge = (
            raw_edge
            - fee
            - settings.half_spread_buffer
            - settings.slippage_buffer
        )
        if net_edge >= settings.minimum_model_edge:
            candidates.append((net_edge, side, probability, price, raw_edge, fee))
    if not candidates:
        return None

    net_edge, side, probability, price, raw_edge, fee = max(candidates)
    notional = _kelly_notional(probability, price, fee, settings)
    if notional <= 0:
        return None
    shares = (notional / price).quantize(Decimal("0.0001"), rounding=ROUND_FLOOR)
    if shares <= 0:
        return None
    notional = price * shares
    return EwmaTwapDecision(
        side=side,
        probability=probability,
        price=price,
        raw_edge=raw_edge,
        fee_per_share=fee,
        net_model_edge=net_edge,
        notional=notional,
        shares=shares,
    )
