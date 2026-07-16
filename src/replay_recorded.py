from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from src.watch_updown import phase_trend


@dataclass(frozen=True)
class Snapshot:
    slug: str
    observed_ts: int
    seconds_left: int
    spot: Decimal
    start_spot: Decimal
    spot_source: str
    probability_up: Decimal
    up_bid: Decimal | None
    up_ask: Decimal | None
    down_bid: Decimal | None
    down_ask: Decimal | None


@dataclass(frozen=True)
class Params:
    trend_threshold: Decimal
    max_entry: Decimal
    min_edge: Decimal


@dataclass(frozen=True)
class Trade:
    slug: str
    side: str
    entry: Decimal
    won: bool
    profit: Decimal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk-forward backtest for recorded Polymarket snapshots.")
    parser.add_argument("path", help="JSONL file produced by watch_updown --record-jsonl.")
    parser.add_argument("--train-ratio", type=float, default=0.70)
    parser.add_argument("--stake", default="1")
    parser.add_argument("--slippage", default="0.01", help="Added to recorded ask.")
    parser.add_argument("--cost-rate", default="0.00", help="Extra cost as a fraction of stake.")
    parser.add_argument("--min-train-trades", type=int, default=20)
    return parser.parse_args()


def _decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def load_windows(path: Path) -> list[list[Snapshot]]:
    grouped: dict[str, list[Snapshot]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            snapshot = Snapshot(
                slug=item["slug"],
                observed_ts=int(item["observed_ts"]),
                seconds_left=int(item["seconds_left"]),
                spot=Decimal(item["spot"]),
                start_spot=Decimal(item["start_spot"]),
                spot_source=item["spot_source"],
                probability_up=Decimal(item["probability_up"]),
                up_bid=_decimal(item.get("up_bid")),
                up_ask=_decimal(item.get("up_ask")),
                down_bid=_decimal(item.get("down_bid")),
                down_ask=_decimal(item.get("down_ask")),
            )
            if snapshot.spot_source == "POLYMARKET_CHAINLINK":
                grouped.setdefault(snapshot.slug, []).append(snapshot)
    windows = [sorted(items, key=lambda item: item.observed_ts) for items in grouped.values()]
    windows.sort(key=lambda items: items[0].observed_ts)
    return [items for items in windows if items[0].seconds_left >= 280 and items[-1].seconds_left <= 15]


def simulate_window(
    snapshots: list[Snapshot],
    params: Params,
    stake: Decimal,
    slippage: Decimal,
    cost_rate: Decimal,
) -> Trade | None:
    first_phase = [item.spot for item in snapshots if 200 < item.seconds_left <= 300]
    second_phase = [item.spot for item in snapshots if 100 < item.seconds_left <= 200]
    candidates = [item for item in snapshots if 40 < item.seconds_left <= 95]
    if len(first_phase) < 2 or len(second_phase) < 2 or not candidates:
        return None
    first, _ = phase_trend(first_phase, params.trend_threshold)
    second, _ = phase_trend(second_phase, params.trend_threshold)
    if first != second or first not in {"U", "D"}:
        return None
    side = "UP" if first == "U" else "DOWN"
    decision = candidates[0]
    ask = decision.up_ask if side == "UP" else decision.down_ask
    bid = decision.up_bid if side == "UP" else decision.down_bid
    probability = decision.probability_up if side == "UP" else Decimal("1") - decision.probability_up
    if ask is None or bid is None or ask < bid or ask - bid > Decimal("0.04"):
        return None
    entry = min(Decimal("0.99"), ask + slippage)
    if entry < Decimal("0.35") or entry > params.max_entry or probability - entry < params.min_edge:
        return None
    winner = "UP" if snapshots[-1].spot > snapshots[-1].start_spot else "DOWN"
    won = side == winner
    shares = stake / entry
    profit = (shares - stake if won else -stake) - stake * cost_rate
    return Trade(snapshots[0].slug, side, entry, won, profit)


def run_params(windows: list[list[Snapshot]], params: Params, stake: Decimal, slippage: Decimal, cost_rate: Decimal) -> list[Trade]:
    return [trade for window in windows if (trade := simulate_window(window, params, stake, slippage, cost_rate))]


def metrics(trades: list[Trade]) -> dict[str, Decimal | int]:
    equity = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    streak = 0
    max_loss_streak = 0
    for trade in trades:
        equity += trade.profit
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        streak = 0 if trade.won else streak + 1
        max_loss_streak = max(max_loss_streak, streak)
    wins = sum(trade.won for trade in trades)
    return {
        "trades": len(trades),
        "win_rate": Decimal(wins) / Decimal(len(trades)) if trades else Decimal("0"),
        "profit": equity,
        "max_drawdown": max_drawdown,
        "max_loss_streak": max_loss_streak,
    }


def main() -> None:
    args = parse_args()
    if not 0 < args.train_ratio < 1:
        raise ValueError("--train-ratio must be between 0 and 1")
    windows = load_windows(Path(args.path))
    split = int(len(windows) * args.train_ratio)
    train, test = windows[:split], windows[split:]
    stake = Decimal(args.stake)
    slippage = Decimal(args.slippage)
    cost_rate = Decimal(args.cost_rate)
    grid = [
        Params(threshold, max_entry, edge)
        for threshold in map(Decimal, ("0.20", "0.35", "0.50"))
        for max_entry in map(Decimal, ("0.60", "0.65", "0.70"))
        for edge in map(Decimal, ("0.03", "0.06", "0.10"))
    ]
    eligible = []
    for params in grid:
        result = run_params(train, params, stake, slippage, cost_rate)
        score = metrics(result)
        if score["trades"] >= args.min_train_trades:
            eligible.append((score["profit"], params, score))
    print(f"quality_windows={len(windows)} train={len(train)} test={len(test)}")
    if not eligible:
        print("No parameter set met --min-train-trades; collect more data before selecting a strategy.")
        return
    _, selected, train_metrics = max(eligible, key=lambda item: item[0])
    test_metrics = metrics(run_params(test, selected, stake, slippage, cost_rate))
    print(f"selected={selected}")
    print(f"train={train_metrics}")
    print(f"test={test_metrics}")


if __name__ == "__main__":
    main()
