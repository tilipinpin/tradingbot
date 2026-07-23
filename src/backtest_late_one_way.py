from __future__ import annotations

import argparse
import csv
import glob
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable


ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class Snapshot:
    observed_ts: int
    seconds_left: int
    spot: Decimal
    start_spot: Decimal
    up_bid: Decimal | None
    up_ask: Decimal | None
    down_bid: Decimal | None
    down_ask: Decimal | None


@dataclass(frozen=True)
class BacktestTrade:
    slug: str
    source: str
    side: str
    winner: str
    entry_seconds_left: int
    entry_ask: Decimal
    entry_price: Decimal
    primary_cost_with_fee: Decimal
    protected: bool
    hedge_seconds_left: int | None
    hedge_ask: Decimal | None
    hedge_price: Decimal | None
    total_cost_with_protection: Decimal
    pnl_without_protection: Decimal
    pnl_with_protection: Decimal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay the late_one_way strategy from recorded market snapshots."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="JSONL paths or glob patterns. For overlapping slugs, the most complete source is used.",
    )
    parser.add_argument("--entry-start-seconds", type=int, default=300)
    parser.add_argument("--entry-cutoff-seconds", type=int, default=1)
    parser.add_argument("--min-entry", default="0.60")
    parser.add_argument("--max-entry", default="0.70")
    parser.add_argument("--trend-samples", type=int, default=5)
    parser.add_argument("--reversal-seconds", type=int, default=5)
    parser.add_argument("--primary-shares", default="5")
    parser.add_argument("--max-primary-spread", default="0.05")
    parser.add_argument("--max-hedge-spread", default="0.10")
    parser.add_argument("--min-ask-sum", default="0.90")
    parser.add_argument("--max-ask-sum", default="1.10")
    parser.add_argument("--max-hedge-entry", default="0.99")
    parser.add_argument("--fee-rate", default="0.07")
    parser.add_argument(
        "--slippage",
        default="0",
        help="Assumed adverse fill slippage per leg; live order limits still apply.",
    )
    parser.add_argument(
        "--require-next-sample",
        action="store_true",
        help="Require the following recorded sample to still qualify before entering.",
    )
    parser.add_argument("--output", default="", help="Optional trade-level CSV output.")
    return parser.parse_args()


def decimal_or_none(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def load_source(path: Path) -> dict[str, list[Snapshot]]:
    windows: dict[str, list[Snapshot]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
                snapshot = Snapshot(
                    observed_ts=int(item["observed_ts"]),
                    seconds_left=int(item["seconds_left"]),
                    spot=Decimal(str(item["spot"])),
                    start_spot=Decimal(str(item["start_spot"])),
                    up_bid=decimal_or_none(item.get("up_bid")),
                    up_ask=decimal_or_none(item.get("up_ask")),
                    down_bid=decimal_or_none(item.get("down_bid")),
                    down_ask=decimal_or_none(item.get("down_ask")),
                )
                windows.setdefault(str(item["slug"]), []).append(snapshot)
            except (ArithmeticError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    for rows in windows.values():
        rows.sort(key=lambda row: (row.observed_ts, -row.seconds_left))
    return windows


def expand_inputs(patterns: Iterable[str]) -> list[Path]:
    paths: dict[str, Path] = {}
    for pattern in patterns:
        matches = glob.glob(pattern)
        if not matches and Path(pattern).is_file():
            matches = [pattern]
        for match in matches:
            path = Path(match)
            if path.is_file():
                paths[str(path.resolve())] = path
    return sorted(paths.values())


def source_score(rows: list[Snapshot]) -> tuple[bool, bool, int, int]:
    has_entry_period = any(1 <= row.seconds_left <= 100 for row in rows)
    reaches_settlement = bool(rows) and min(row.seconds_left for row in rows) <= 5
    covered_seconds = (
        max(row.observed_ts for row in rows) - min(row.observed_ts for row in rows)
        if rows
        else 0
    )
    return reaches_settlement, has_entry_period, covered_seconds, len(rows)


def select_windows(paths: list[Path]) -> dict[str, tuple[str, list[Snapshot]]]:
    candidates: dict[str, list[tuple[str, list[Snapshot]]]] = {}
    for path in paths:
        for slug, rows in load_source(path).items():
            candidates.setdefault(slug, []).append((path.name, rows))
    return {
        slug: max(sources, key=lambda source: source_score(source[1]))
        for slug, sources in candidates.items()
    }


def quote_sanity(
    row: Snapshot,
    max_spread: Decimal,
    min_ask_sum: Decimal,
    max_ask_sum: Decimal,
) -> bool:
    if None in (row.up_bid, row.up_ask, row.down_bid, row.down_ask):
        return False
    assert row.up_bid is not None and row.up_ask is not None
    assert row.down_bid is not None and row.down_ask is not None
    return (
        row.up_ask >= row.up_bid
        and row.down_ask >= row.down_bid
        and row.up_ask - row.up_bid <= max_spread
        and row.down_ask - row.down_bid <= max_spread
        and min_ask_sum <= row.up_ask + row.down_ask <= max_ask_sum
    )


def trend_side(
    up_asks: list[Decimal],
    down_asks: list[Decimal],
    samples: int,
) -> str | None:
    if samples < 2 or len(up_asks) < samples or len(down_asks) < samples:
        return None
    up = up_asks[-samples:]
    down = down_asks[-samples:]
    if all(now >= before for before, now in zip(up, up[1:])) and all(
        now <= before for before, now in zip(down, down[1:])
    ):
        return "UP"
    if all(now >= before for before, now in zip(down, down[1:])) and all(
        now <= before for before, now in zip(up, up[1:])
    ):
        return "DOWN"
    return None


def entry_side(
    row: Snapshot,
    up_asks: list[Decimal],
    down_asks: list[Decimal],
    args: argparse.Namespace,
) -> str | None:
    if not args.entry_cutoff_seconds <= row.seconds_left <= args.entry_start_seconds:
        return None
    if not quote_sanity(
        row,
        Decimal(args.max_primary_spread),
        Decimal(args.min_ask_sum),
        Decimal(args.max_ask_sum),
    ):
        return None
    side = trend_side(up_asks, down_asks, args.trend_samples)
    if side is None:
        return None
    if (side == "UP" and row.spot <= row.start_spot) or (
        side == "DOWN" and row.spot >= row.start_spot
    ):
        return None
    ask = row.up_ask if side == "UP" else row.down_ask
    if ask is None or not Decimal(args.min_entry) <= ask <= Decimal(args.max_entry):
        return None
    return side


def fee(shares: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    return shares * fee_rate * price * (ONE - price)


def hedge_reduces_max_loss(
    primary_side: str,
    primary_cost: Decimal,
    primary_shares: Decimal,
    hedge_price: Decimal,
    hedge_shares: Decimal,
    fee_rate: Decimal,
) -> bool:
    primary_fee = fee(primary_shares, primary_cost / primary_shares, fee_rate)
    hedge_fee = fee(hedge_shares, hedge_price, fee_rate)
    total_cost = primary_cost + primary_fee + hedge_price * hedge_shares + hedge_fee
    payout_up = (
        primary_shares if primary_side == "UP" else hedge_shares
    )
    payout_down = (
        primary_shares if primary_side == "DOWN" else hedge_shares
    )
    max_loss_after = max(ZERO, -min(payout_up - total_cost, payout_down - total_cost))
    return max_loss_after < primary_cost + primary_fee


def replay_window(
    slug: str,
    source: str,
    rows: list[Snapshot],
    args: argparse.Namespace,
) -> BacktestTrade | None:
    if not rows or min(row.seconds_left for row in rows) > 5:
        return None
    settlement_row = min(rows, key=lambda row: row.seconds_left)
    if settlement_row.spot == settlement_row.start_spot:
        return None
    winner = "UP" if settlement_row.spot > settlement_row.start_spot else "DOWN"

    up_asks: list[Decimal] = []
    down_asks: list[Decimal] = []
    entry_index: int | None = None
    side: str | None = None
    for index, row in enumerate(rows):
        if row.up_ask is not None and row.down_ask is not None:
            up_asks.append(row.up_ask)
            down_asks.append(row.down_ask)
        candidate = entry_side(row, up_asks, down_asks, args)
        if candidate is None:
            continue
        if args.require_next_sample:
            if index + 1 >= len(rows):
                continue
            next_row = rows[index + 1]
            next_up = [*up_asks, *([next_row.up_ask] if next_row.up_ask is not None else [])]
            next_down = [
                *down_asks,
                *([next_row.down_ask] if next_row.down_ask is not None else []),
            ]
            if entry_side(next_row, next_up, next_down, args) != candidate:
                continue
            index += 1
            row = next_row
        entry_index = index
        side = candidate
        break
    if entry_index is None or side is None:
        return None

    entry_row = rows[entry_index]
    entry_ask = entry_row.up_ask if side == "UP" else entry_row.down_ask
    assert entry_ask is not None
    slippage = Decimal(args.slippage)
    entry_price = min(Decimal(args.max_entry), entry_ask + slippage)
    primary_shares = Decimal(args.primary_shares)
    primary_cost = entry_price * primary_shares
    fee_rate = Decimal(args.fee_rate)
    primary_fee = fee(primary_shares, entry_price, fee_rate)
    payout_without = primary_shares if winner == side else ZERO
    pnl_without = payout_without - primary_cost - primary_fee

    reverse_side = "DOWN" if side == "UP" else "UP"
    reversal_started_at: int | None = None
    hedge_row: Snapshot | None = None
    hedge_price: Decimal | None = None
    for row in rows[entry_index + 1 :]:
        crossed = (
            row.spot < row.start_spot
            if reverse_side == "DOWN"
            else row.spot > row.start_spot
        )
        if not crossed:
            reversal_started_at = None
            continue
        if reversal_started_at is None:
            reversal_started_at = row.observed_ts
        if row.observed_ts - reversal_started_at < args.reversal_seconds:
            continue
        if row.seconds_left < args.entry_cutoff_seconds:
            continue
        if not quote_sanity(
            row,
            Decimal(args.max_hedge_spread),
            Decimal(args.min_ask_sum),
            Decimal(args.max_ask_sum),
        ):
            continue
        hedge_ask = row.down_ask if reverse_side == "DOWN" else row.up_ask
        if hedge_ask is None or hedge_ask <= ZERO or hedge_ask > Decimal(args.max_hedge_entry):
            continue
        candidate_price = min(ONE, hedge_ask + slippage)
        hedge_shares = primary_cost / candidate_price
        if not hedge_reduces_max_loss(
            side,
            primary_cost,
            primary_shares,
            candidate_price,
            hedge_shares,
            fee_rate,
        ):
            continue
        hedge_row = row
        hedge_price = candidate_price
        break

    pnl_with = pnl_without
    total_cost_with_protection = primary_cost + primary_fee
    if hedge_row is not None and hedge_price is not None:
        hedge_shares = primary_cost / hedge_price
        hedge_fee = fee(hedge_shares, hedge_price, fee_rate)
        total_cost_with_protection += primary_cost + hedge_fee
        hedge_payout = hedge_shares if winner == reverse_side else ZERO
        pnl_with = (
            payout_without
            + hedge_payout
            - primary_cost
            - primary_fee
            - primary_cost
            - hedge_fee
        )

    return BacktestTrade(
        slug=slug,
        source=source,
        side=side,
        winner=winner,
        entry_seconds_left=entry_row.seconds_left,
        entry_ask=entry_ask,
        entry_price=entry_price,
        primary_cost_with_fee=primary_cost + primary_fee,
        protected=hedge_row is not None,
        hedge_seconds_left=hedge_row.seconds_left if hedge_row is not None else None,
        hedge_ask=(
            hedge_row.down_ask if side == "UP" else hedge_row.up_ask
        )
        if hedge_row is not None
        else None,
        hedge_price=hedge_price,
        total_cost_with_protection=total_cost_with_protection,
        pnl_without_protection=pnl_without,
        pnl_with_protection=pnl_with,
    )


def maximum_drawdown(profits: Iterable[Decimal]) -> Decimal:
    equity = ZERO
    peak = ZERO
    drawdown = ZERO
    for profit in profits:
        equity += profit
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def summarize(windows: int, trades: list[BacktestTrade]) -> dict[str, object]:
    wins = sum(trade.side == trade.winner for trade in trades)
    protected = sum(trade.protected for trade in trades)
    without = [trade.pnl_without_protection for trade in trades]
    with_protection = [trade.pnl_with_protection for trade in trades]
    primary_capital = sum((trade.primary_cost_with_fee for trade in trades), ZERO)
    protected_capital = sum(
        (trade.total_cost_with_protection for trade in trades),
        ZERO,
    )
    pnl_without = sum(without, ZERO)
    pnl_with = sum(with_protection, ZERO)
    return {
        "resolved_windows": windows,
        "trades": len(trades),
        "participation_rate": str(Decimal(len(trades)) / windows) if windows else "0",
        "wins": wins,
        "win_rate": str(Decimal(wins) / len(trades)) if trades else "0",
        "protected_trades": protected,
        "protection_rate": str(Decimal(protected) / len(trades)) if trades else "0",
        "profitable_trades_with_protection": sum(profit > 0 for profit in with_protection),
        "primary_capital_with_fees": str(primary_capital),
        "total_capital_with_protection": str(protected_capital),
        "pnl_without_protection": str(pnl_without),
        "pnl_with_protection": str(pnl_with),
        "return_on_primary_capital": str(pnl_without / primary_capital)
        if primary_capital
        else "0",
        "return_on_total_protected_capital": str(pnl_with / protected_capital)
        if protected_capital
        else "0",
        "average_pnl_with_protection": str(pnl_with / len(trades)) if trades else "0",
        "protection_pnl_delta": str(pnl_with - pnl_without),
        "max_drawdown_without_protection": str(maximum_drawdown(without)),
        "max_drawdown_with_protection": str(maximum_drawdown(with_protection)),
        "average_entry": str(
            sum((trade.entry_price for trade in trades), ZERO) / len(trades)
        )
        if trades
        else "0",
    }


def write_csv(path: Path, trades: list[BacktestTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(trades[0]).keys()) if trades else list(BacktestTrade.__annotations__)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for trade in trades:
            writer.writerow(asdict(trade))


def run() -> None:
    args = parse_args()
    paths = expand_inputs(args.inputs)
    if not paths:
        raise SystemExit("No input snapshot files matched.")
    windows = select_windows(paths)
    trades = [
        trade
        for slug, (source, rows) in sorted(windows.items())
        if (trade := replay_window(slug, source, rows, args)) is not None
    ]
    resolved_windows = sum(
        bool(rows) and min(row.seconds_left for row in rows) <= 5
        for _, rows in windows.values()
    )
    output = {
        "inputs": [str(path) for path in paths],
        "parameters": {
            "entry_seconds": [args.entry_start_seconds, args.entry_cutoff_seconds],
            "entry_range": [args.min_entry, args.max_entry],
            "trend_samples": args.trend_samples,
            "reversal_seconds": args.reversal_seconds,
            "primary_shares": args.primary_shares,
            "fee_rate": args.fee_rate,
            "slippage": args.slippage,
            "require_next_sample": args.require_next_sample,
        },
        "summary": summarize(resolved_windows, trades),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if args.output:
        write_csv(Path(args.output), trades)


if __name__ == "__main__":
    run()
