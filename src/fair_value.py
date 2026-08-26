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
    sample_times: list[float] | None = None,
) -> Decimal:
    if len(prices) < 3:
        return fallback_sigma

    if sample_times is not None and len(sample_times) != len(prices):
        raise ValueError("sample_times must match prices")

    timed_returns: list[tuple[float, float]] = []
    for index, (previous, current) in enumerate(zip(prices, prices[1:])):
        if previous <= 0 or current <= 0:
            continue
        elapsed = (
            sample_times[index + 1] - sample_times[index]
            if sample_times is not None
            else float(interval_seconds)
        )
        if elapsed <= 0:
            continue
        timed_returns.append((math.log(float(current / previous)), elapsed))

    if len(timed_returns) < 2:
        return fallback_sigma

    if sample_times is None:
        returns = [value for value, _ in timed_returns]
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        sigma_per_interval = Decimal(str(math.sqrt(variance)))
        if interval_seconds <= 0:
            return fallback_sigma
        sigma = sigma_per_interval / Decimal(str(math.sqrt(float(interval_seconds))))
    else:
        total_seconds = sum(elapsed for _, elapsed in timed_returns)
        if total_seconds <= 0:
            return fallback_sigma
        drift_per_second = sum(value for value, _ in timed_returns) / total_seconds
        variance_rate = sum(
            (value - drift_per_second * elapsed) ** 2
            for value, elapsed in timed_returns
        ) / total_seconds
        sigma = Decimal(str(math.sqrt(max(0.0, variance_rate))))

    if sigma <= 0:
        return fallback_sigma
    # Treat the fallback as a long-run volatility floor. A quiet handful of
    # samples must not collapse the denominator and create false 0%/100% odds.
    return max(sigma, fallback_sigma)


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


def twap_effective_variance_seconds(
    seconds_to_expiry: Decimal,
    window_seconds: Decimal = Decimal("60"),
) -> Decimal:
    """Brownian variance clock for a future rolling-window average."""
    remaining = max(Decimal("0"), seconds_to_expiry)
    if window_seconds <= 0:
        raise ValueError("TWAP window must be positive")
    if remaining >= window_seconds:
        return remaining - Decimal("2") * window_seconds / Decimal("3")
    return remaining**3 / (Decimal("3") * window_seconds**2)


def trailing_time_weighted_average(
    prices: list[Decimal],
    sample_times: list[float],
    lookback_seconds: Decimal,
) -> Decimal | None:
    """Trapezoidal average over an exact trailing interval, with interpolation."""
    if len(prices) != len(sample_times):
        raise ValueError("sample_times must match prices")
    if len(prices) < 2 or lookback_seconds <= 0:
        return None
    end = sample_times[-1]
    start = end - float(lookback_seconds)
    if sample_times[0] > start:
        return None

    integral = Decimal("0")
    covered = 0.0
    for index, (left_time, right_time) in enumerate(
        zip(sample_times, sample_times[1:])
    ):
        if right_time <= left_time or right_time <= start or left_time >= end:
            continue
        overlap_left = max(left_time, start)
        overlap_right = min(right_time, end)
        if overlap_right <= overlap_left:
            continue
        left_price = prices[index]
        right_price = prices[index + 1]
        duration = right_time - left_time
        left_fraction = Decimal(str((overlap_left - left_time) / duration))
        right_fraction = Decimal(str((overlap_right - left_time) / duration))
        price_at_left = left_price + (right_price - left_price) * left_fraction
        price_at_right = left_price + (right_price - left_price) * right_fraction
        overlap_duration = Decimal(str(overlap_right - overlap_left))
        integral += (price_at_left + price_at_right) * overlap_duration / Decimal("2")
        covered += overlap_right - overlap_left

    if covered + 1e-6 < float(lookback_seconds):
        return None
    return integral / lookback_seconds


def btc_up_twap_probability(
    start_twap: Decimal,
    current_twap: Decimal,
    current_spot: Decimal,
    seconds_to_expiry: Decimal,
    sigma_per_sqrt_second: Decimal,
    window_seconds: Decimal = Decimal("60"),
    known_overlap_average: Decimal | None = None,
) -> FairValueResult:
    """Estimate the probability that the expiry TWAP beats the opening TWAP.

    The current rolling TWAP is projected toward spot as its old observations
    roll out. Uncertainty uses the integrated-Brownian variance of the future
    portion of the final TWAP window rather than terminal spot variance.
    """
    if min(start_twap, current_twap, current_spot) <= 0:
        raise ValueError("TWAP and spot prices must be positive")
    remaining = max(Decimal("0"), seconds_to_expiry)
    if remaining <= 0:
        return btc_up_probability(
            start_twap,
            current_twap,
            Decimal("0"),
            sigma_per_sqrt_second,
        )
    if window_seconds <= 0:
        raise ValueError("TWAP window must be positive")
    roll_fraction = min(Decimal("1"), remaining / window_seconds)
    if known_overlap_average is not None and remaining < window_seconds:
        if known_overlap_average <= 0:
            raise ValueError("Known TWAP overlap average must be positive")
        overlap_seconds = window_seconds - remaining
        projected_twap = (
            known_overlap_average * overlap_seconds + current_spot * remaining
        ) / window_seconds
    else:
        projected_twap = current_twap + roll_fraction * (current_spot - current_twap)
    return btc_up_probability(
        start_twap,
        projected_twap,
        twap_effective_variance_seconds(remaining, window_seconds),
        sigma_per_sqrt_second,
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
