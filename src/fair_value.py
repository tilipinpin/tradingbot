from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FairValueResult:
    probability_up: Decimal
    probability_down: Decimal
    z_score: Decimal
    sigma_per_sqrt_second: Decimal


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def estimate_sigma_per_sqrt_second(
    prices: list[Decimal],
    interval_seconds: Decimal,
    fallback_sigma: Decimal,
) -> Decimal:
    if len(prices) < 3:
        return fallback_sigma

    returns: list[float] = []
    for previous, current in zip(prices, prices[1:]):
        if previous <= 0 or current <= 0:
            continue
        returns.append(math.log(float(current / previous)))

    if len(returns) < 2:
        return fallback_sigma

    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    sigma_per_interval = Decimal(str(math.sqrt(variance)))
    if interval_seconds <= 0:
        return fallback_sigma
    sigma = sigma_per_interval / Decimal(str(math.sqrt(float(interval_seconds))))
    return max(sigma, fallback_sigma / Decimal("10"))


def btc_up_probability(
    start_price: Decimal,
    current_price: Decimal,
    seconds_to_expiry: Decimal,
    sigma_per_sqrt_second: Decimal,
) -> FairValueResult:
    if start_price <= 0 or current_price <= 0:
        raise ValueError("start_price and current_price must be positive")
    if seconds_to_expiry <= 0:
        probability_up = Decimal("1") if current_price >= start_price else Decimal("0")
        return FairValueResult(
            probability_up=probability_up,
            probability_down=Decimal("1") - probability_up,
            z_score=Decimal("0"),
            sigma_per_sqrt_second=sigma_per_sqrt_second,
        )

    denominator = sigma_per_sqrt_second * Decimal(str(math.sqrt(float(seconds_to_expiry))))
    if denominator <= 0:
        probability_up = Decimal("1") if current_price >= start_price else Decimal("0")
        return FairValueResult(
            probability_up=probability_up,
            probability_down=Decimal("1") - probability_up,
            z_score=Decimal("0"),
            sigma_per_sqrt_second=sigma_per_sqrt_second,
        )

    log_distance = Decimal(str(math.log(float(current_price / start_price))))
    z_score = log_distance / denominator
    probability = Decimal(str(normal_cdf(float(z_score))))
    probability = min(max(probability, Decimal("0")), Decimal("1"))
    return FairValueResult(
        probability_up=probability,
        probability_down=Decimal("1") - probability,
        z_score=z_score,
        sigma_per_sqrt_second=sigma_per_sqrt_second,
    )


def choose_theoretical_action(
    probability_up: Decimal,
    up_ask: Decimal | None,
    down_ask: Decimal | None,
    edge_threshold: Decimal,
) -> str:
    actions: list[tuple[Decimal, str]] = []
    if up_ask is not None:
        actions.append((probability_up - up_ask, "BUY_UP"))
    if down_ask is not None:
        actions.append(((Decimal("1") - probability_up) - down_ask, "BUY_DOWN"))
    if not actions:
        return "NO_MARKET_QUOTE"

    edge, action = max(actions, key=lambda item: item[0])
    if edge >= edge_threshold:
        return f"{action} edge={edge:.4f}"
    return f"SKIP best_edge={edge:.4f}"
