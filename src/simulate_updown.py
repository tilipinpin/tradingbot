from __future__ import annotations

import argparse
import logging
import time
from decimal import Decimal

from src.fair_value import btc_up_probability, choose_theoretical_action, estimate_sigma_per_sqrt_second
from src.price_signal import SpotPriceClient


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("btc-updown-sim")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run BTC 5m Up/Down fair-value simulator.")
    parser.add_argument("--duration", type=int, default=300, help="How long to simulate, in seconds.")
    parser.add_argument("--interval", type=int, default=15, help="Polling interval, in seconds.")
    parser.add_argument("--window", type=int, default=300, help="Simulated market length, in seconds.")
    parser.add_argument("--price-source", default="COINGECKO", help="BINANCE, COINBASE, or COINGECKO.")
    parser.add_argument("--edge", default="0.06", help="Minimum theoretical edge for BUY_UP/BUY_DOWN.")
    parser.add_argument(
        "--fallback-sigma",
        default="0.00005",
        help="Fallback BTC volatility per sqrt(second), used before enough samples exist.",
    )
    parser.add_argument("--up-ask", default=None, help="Optional simulated UP ask price, e.g. 0.52.")
    parser.add_argument("--down-ask", default=None, help="Optional simulated DOWN ask price, e.g. 0.52.")
    return parser.parse_args()


def _decimal_or_none(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def run() -> None:
    args = parse_args()
    source = SpotPriceClient(args.price_source)
    edge_threshold = Decimal(args.edge)
    fallback_sigma = Decimal(args.fallback_sigma)
    up_ask = _decimal_or_none(args.up_ask)
    down_ask = _decimal_or_none(args.down_ask)

    first_price = source.btc_usd().price
    start_price = first_price
    prices = [first_price]
    started_at = time.monotonic()
    expires_at = started_at + args.window
    stop_at = started_at + args.duration

    logger.info(
        "Simulation started: start_price=%s window=%ss duration=%ss source=%s",
        start_price,
        args.window,
        args.duration,
        args.price_source.upper(),
    )

    while True:
        now = time.monotonic()
        if now >= stop_at:
            break

        spot = source.btc_usd()
        prices.append(spot.price)
        elapsed = Decimal(str(now - started_at))
        seconds_to_expiry = max(Decimal("0"), Decimal(args.window) - elapsed)
        sigma = estimate_sigma_per_sqrt_second(prices, Decimal(args.interval), fallback_sigma)
        fair = btc_up_probability(start_price, spot.price, seconds_to_expiry, sigma)
        action = choose_theoretical_action(fair.probability_up, up_ask, down_ask, edge_threshold)

        logger.info(
            "t=%ss spot=%s start=%s seconds_left=%s p_up=%.4f p_down=%.4f z=%.4f sigma=%s action=%s",
            int(elapsed),
            spot.price,
            start_price,
            int(seconds_to_expiry),
            float(fair.probability_up),
            float(fair.probability_down),
            float(fair.z_score),
            fair.sigma_per_sqrt_second,
            action,
        )

        sleep_for = min(args.interval, max(0, stop_at - time.monotonic()))
        if sleep_for <= 0:
            break
        time.sleep(sleep_for)

    final_price = prices[-1]
    result = "UP" if final_price >= start_price else "DOWN"
    logger.info("Simulation finished: start=%s final=%s result=%s samples=%s", start_price, final_price, result, len(prices))


if __name__ == "__main__":
    run()
