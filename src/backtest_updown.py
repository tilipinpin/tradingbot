from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import requests


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("btc-updown-backtest")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
SLUG_PATTERN = re.compile(r"^(btc-updown-5m-)(\d+)$")


@dataclass(frozen=True)
class PricePoint:
    timestamp: int
    price: Decimal


@dataclass(frozen=True)
class WindowData:
    slug: str
    start_ts: int
    end_ts: int
    winner: str
    up_prices: list[PricePoint]
    down_prices: list[PricePoint]


@dataclass(frozen=True)
class TradeResult:
    slug: str
    strategy: str
    side: str
    entry_price: Decimal
    winner: str
    won: bool
    profit: Decimal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest BTC 5m Up/Down strategies using Polymarket price history.")
    parser.add_argument("--latest-slug", required=True, help="Newest known btc-updown-5m-* slug.")
    parser.add_argument("--windows", type=int, default=100, help="How many previous 5m windows to test.")
    parser.add_argument("--decision-seconds-before-end", type=int, default=60)
    parser.add_argument("--max-entry", default="0.95", help="Skip trades above this entry price.")
    parser.add_argument("--min-entry", default="0.02", help="Skip trades below this entry price.")
    parser.add_argument("--fee-rate", default="0", help="Approx taker fee rate applied to min(price, 1-price).")
    parser.add_argument("--output", default="", help="Optional CSV path for trade-level results.")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent fetch workers.")
    return parser.parse_args()


def slug_timestamp(slug: str) -> int:
    match = SLUG_PATTERN.match(slug)
    if not match:
        raise ValueError(f"Not a btc-updown-5m slug: {slug}")
    return int(match.group(2))


def slug_for_timestamp(timestamp: int) -> str:
    return f"btc-updown-5m-{timestamp}"


def previous_slugs(latest_slug: str, windows: int) -> list[str]:
    latest = slug_timestamp(latest_slug)
    return [slug_for_timestamp(latest - 300 * index) for index in range(windows)]


def parse_history(payload: dict) -> list[PricePoint]:
    points = []
    for item in payload.get("history", []):
        points.append(PricePoint(timestamp=int(item["t"]), price=Decimal(str(item["p"]))))
    return sorted(points, key=lambda point: point.timestamp)


def price_at_or_before(points: list[PricePoint], timestamp: int) -> Decimal | None:
    eligible = [point for point in points if point.timestamp <= timestamp]
    if eligible:
        return eligible[-1].price
    return points[0].price if points else None


def winning_side(outcome_prices: str) -> str | None:
    try:
        prices = json.loads(outcome_prices)
    except json.JSONDecodeError:
        return None
    if len(prices) < 2:
        return None
    up = Decimal(str(prices[0]))
    down = Decimal(str(prices[1]))
    if up == Decimal("1"):
        return "UP"
    if down == Decimal("1"):
        return "DOWN"
    return None


def fetch_window(slug: str) -> WindowData | None:
    event_response = requests.get(f"{GAMMA_API}/events", params={"slug": slug, "limit": 1}, timeout=20)
    event_response.raise_for_status()
    events = event_response.json()
    if not events:
        return None
    markets = events[0].get("markets") or []
    if not markets:
        return None
    market = markets[0]
    winner = winning_side(market.get("outcomePrices") or "")
    if winner is None:
        return None
    token_ids = json.loads(market["clobTokenIds"])
    start_ts = int(slug_timestamp(slug))
    end_ts = start_ts + 300
    up_history = requests.get(
        f"{CLOB_API}/prices-history",
        params={"market": token_ids[0], "startTs": start_ts, "endTs": end_ts},
        timeout=20,
    )
    up_history.raise_for_status()
    down_history = requests.get(
        f"{CLOB_API}/prices-history",
        params={"market": token_ids[1], "startTs": start_ts, "endTs": end_ts},
        timeout=20,
    )
    down_history.raise_for_status()
    return WindowData(
        slug=slug,
        start_ts=start_ts,
        end_ts=end_ts,
        winner=winner,
        up_prices=parse_history(up_history.json()),
        down_prices=parse_history(down_history.json()),
    )


def approximate_fee(entry_price: Decimal, fee_rate: Decimal) -> Decimal:
    return fee_rate * min(entry_price, Decimal("1") - entry_price)


def simulate_trade(
    window: WindowData,
    strategy: str,
    decision_ts: int,
    min_entry: Decimal,
    max_entry: Decimal,
    fee_rate: Decimal,
) -> TradeResult | None:
    up_now = price_at_or_before(window.up_prices, decision_ts)
    down_now = price_at_or_before(window.down_prices, decision_ts)
    up_start = price_at_or_before(window.up_prices, window.start_ts + 30)
    if up_now is None or down_now is None or up_start is None:
        return None

    if strategy == "favorite":
        side = "UP" if up_now >= down_now else "DOWN"
    elif strategy == "underdog":
        side = "UP" if up_now < down_now else "DOWN"
    elif strategy == "momentum":
        side = "UP" if up_now >= up_start else "DOWN"
    elif strategy == "mean_reversion":
        side = "DOWN" if up_now >= up_start else "UP"
    elif strategy == "strong_favorite":
        if max(up_now, down_now) < Decimal("0.60"):
            return None
        side = "UP" if up_now >= down_now else "DOWN"
    else:
        raise ValueError(f"Unknown strategy: {strategy}")

    entry = up_now if side == "UP" else down_now
    if entry < min_entry or entry > max_entry:
        return None
    won = side == window.winner
    fee = approximate_fee(entry, fee_rate)
    profit = (Decimal("1") - entry - fee) if won else (-entry - fee)
    return TradeResult(
        slug=window.slug,
        strategy=strategy,
        side=side,
        entry_price=entry,
        winner=window.winner,
        won=won,
        profit=profit,
    )


def summarize(results: list[TradeResult]) -> dict[str, dict[str, Decimal]]:
    summary: dict[str, dict[str, Decimal]] = {}
    for result in results:
        item = summary.setdefault(
            result.strategy,
            {"trades": Decimal("0"), "wins": Decimal("0"), "profit": Decimal("0"), "avg_entry": Decimal("0")},
        )
        item["trades"] += Decimal("1")
        item["wins"] += Decimal("1") if result.won else Decimal("0")
        item["profit"] += result.profit
        item["avg_entry"] += result.entry_price
    for item in summary.values():
        if item["trades"] > 0:
            item["win_rate"] = item["wins"] / item["trades"]
            item["avg_profit"] = item["profit"] / item["trades"]
            item["avg_entry"] = item["avg_entry"] / item["trades"]
    return summary


def write_csv(path: Path, results: list[TradeResult]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["slug", "strategy", "side", "entry_price", "winner", "won", "profit"])
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "slug": result.slug,
                    "strategy": result.strategy,
                    "side": result.side,
                    "entry_price": result.entry_price,
                    "winner": result.winner,
                    "won": result.won,
                    "profit": result.profit,
                }
            )


def run() -> None:
    args = parse_args()
    decision_offset = args.decision_seconds_before_end
    min_entry = Decimal(args.min_entry)
    max_entry = Decimal(args.max_entry)
    fee_rate = Decimal(args.fee_rate)
    strategies = ["favorite", "underdog", "momentum", "mean_reversion", "strong_favorite"]
    results: list[TradeResult] = []
    loaded = 0

    slugs = previous_slugs(args.latest_slug, args.windows)
    windows: list[WindowData] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_to_slug = {executor.submit(fetch_window, slug): slug for slug in slugs}
        for future in as_completed(future_to_slug):
            slug = future_to_slug[future]
            try:
                window = future.result()
            except requests.RequestException as exc:
                logger.warning("Skip %s; fetch failed: %s", slug, exc)
                continue
            if window is not None:
                windows.append(window)

    for window in sorted(windows, key=lambda item: item.start_ts):
        loaded += 1
        decision_ts = window.end_ts - decision_offset
        for strategy in strategies:
            result = simulate_trade(window, strategy, decision_ts, min_entry, max_entry, fee_rate)
            if result is not None:
                results.append(result)

    logger.info("Loaded %s resolved windows; simulated %s trades", loaded, len(results))
    for strategy, item in sorted(summarize(results).items()):
        logger.info(
            "%s trades=%s win_rate=%.2f%% profit=%s avg_profit=%s avg_entry=%s",
            strategy,
            int(item["trades"]),
            float(item["win_rate"] * Decimal("100")),
            item["profit"].quantize(Decimal("0.0001")),
            item["avg_profit"].quantize(Decimal("0.0001")),
            item["avg_entry"].quantize(Decimal("0.0001")),
        )
    if args.output:
        write_csv(Path(args.output), results)
        logger.info("Wrote trade results to %s", args.output)


if __name__ == "__main__":
    run()
