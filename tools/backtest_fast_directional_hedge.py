from __future__ import annotations

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from src.fast_directional_hedge_simple import (
    FastDirectionalHedgeSimpleEngine,
    FastDirectionalHedgeSimpleSettings,
)
from src.polymarket import OrderBookLevel, OrderBookSnapshot


LINE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) INFO "
    r"(?P<slug>btc-updown-5m-\d+) seconds_left=(?P<left>\d+) "
    r"settlement_price=(?P<settlement>[0-9.]+) underlying_spot=(?P<spot>[0-9.]+) "
    r"start=(?P<strike>[0-9.]+) fair_model=\S+ p_up=(?P<p_up>[0-9.]+) "
    r"sigma=(?P<sigma>[0-9.]+) "
    r"up=OrderBookQuote\(bid=Decimal\('(?P<up_bid>[0-9.]+)'\), "
    r"ask=Decimal\('(?P<up_ask>[0-9.]+)'\)\) "
    r"down=OrderBookQuote\(bid=Decimal\('(?P<down_bid>[0-9.]+)'\), "
    r"ask=Decimal\('(?P<down_ask>[0-9.]+)'\)\)"
)


@dataclass(frozen=True)
class Tick:
    observed_at: float
    slug: str
    seconds_left: Decimal
    settlement_price: Decimal
    spot: Decimal
    strike: Decimal
    probability_up: Decimal
    sigma: Decimal
    up_bid: Decimal
    up_ask: Decimal
    down_bid: Decimal
    down_ask: Decimal


def parse_ticks(path: Path) -> list[Tick]:
    local_tz = timezone(timedelta(hours=8))
    ticks: list[Tick] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = LINE.search(line)
        if match is None:
            continue
        timestamp = datetime.strptime(match["time"], "%Y-%m-%d %H:%M:%S,%f").replace(
            tzinfo=local_tz
        )
        ticks.append(
            Tick(
                observed_at=timestamp.timestamp(),
                slug=match["slug"],
                seconds_left=Decimal(match["left"]),
                settlement_price=Decimal(match["settlement"]),
                spot=Decimal(match["spot"]),
                strike=Decimal(match["strike"]),
                probability_up=Decimal(match["p_up"]),
                sigma=Decimal(match["sigma"]),
                up_bid=Decimal(match["up_bid"]),
                up_ask=Decimal(match["up_ask"]),
                down_bid=Decimal(match["down_bid"]),
                down_ask=Decimal(match["down_ask"]),
            )
        )
    return ticks


def book(token: str, bid: Decimal, ask: Decimal, observed_at: float) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token,
        timestamp=str(int(observed_at * 1000)),
        bids=(OrderBookLevel(bid, Decimal("20")),) if bid > 0 else (),
        asks=(OrderBookLevel(ask, Decimal("20")),) if ask > 0 else (),
        minimum_order_size=Decimal("1"),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "log",
        nargs="?",
        type=Path,
        default=Path("data/fast_directional_hedge_paper.stderr.log"),
    )
    parser.add_argument("--entry-min", type=Decimal, default=Decimal("0.53"))
    parser.add_argument("--entry-max", type=Decimal, default=Decimal("0.60"))
    parser.add_argument("--entry-confirm-ticks", type=int, default=2)
    parser.add_argument("--size", type=Decimal, default=Decimal("2"))
    args = parser.parse_args()
    ticks = parse_ticks(args.log)
    by_slug: dict[str, list[Tick]] = defaultdict(list)
    for tick in ticks:
        by_slug[tick.slug].append(tick)

    completed = {
        slug: rows
        for slug, rows in by_slug.items()
        if min(row.seconds_left for row in rows) <= 1
    }
    settings = FastDirectionalHedgeSimpleSettings(
        entry_price_min=args.entry_min,
        entry_price_max=args.entry_max,
        entry_confirm_ticks=args.entry_confirm_ticks,
        base_position_size=args.size,
    )
    engine = FastDirectionalHedgeSimpleEngine(settings)
    window_results: list[dict[str, object]] = []
    for slug, rows in sorted(completed.items(), key=lambda item: item[1][0].observed_at):
        prices: list[Decimal] = []
        times: list[float] = []
        fills: list[tuple[str, Decimal, Decimal, Decimal, str]] = []
        for row in rows:
            prices.append(row.spot)
            times.append(row.observed_at)
            decision = engine.evaluate(
                slug=slug,
                seconds_to_expiry=row.seconds_left,
                up_book=book("UP", row.up_bid, row.up_ask, row.observed_at),
                down_book=book("DOWN", row.down_bid, row.down_ask, row.observed_at),
                observed_at=row.observed_at,
            )
            if decision is None:
                continue
            cost = decision.quantity * decision.limit_price
            fee = decision.quantity * engine.settings.fee_rate * decision.limit_price * (
                Decimal("1") - decision.limit_price
            )
            fills.append(
                (
                    decision.side,
                    decision.quantity,
                    decision.limit_price,
                    fee,
                    "STOP" if decision.role == "HEDGE" else "ENTRY",
                )
            )
            engine.record_fill(
                slug,
                decision.side,
                decision.quantity,
                cost,
            )

        final = min(rows, key=lambda row: row.seconds_left)
        # The recorded local TWAP estimate can disagree with Gamma's formal
        # outcome.  Near expiry the resolved side trades close to one while the
        # losing side trades close to zero, so use the terminal market state as
        # the label instead of feeding the model's own estimate back as truth.
        winner = (
            "UP"
            if (final.up_bid + final.up_ask) >= (final.down_bid + final.down_ask)
            else "DOWN"
        )
        payout = sum(quantity for side, quantity, _, _, _ in fills if side == winner)
        total_cost = sum(quantity * price + fee for _, quantity, price, fee, _ in fills)
        pnl = payout - total_cost
        if fills:
            window_results.append(
                {
                    "slug": slug,
                    "winner": winner,
                    "fills": fills,
                    "pnl": pnl,
                    "entry_correct": fills[0][0] == winner,
                    "stopped": any(role == "STOP" for *_, role in fills),
                }
            )

    cumulative = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for result in window_results:
        cumulative += result["pnl"]  # type: ignore[operator]
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    wins = sum(result["pnl"] > 0 for result in window_results)
    losses = sum(result["pnl"] < 0 for result in window_results)
    stops = sum(bool(result["stopped"]) for result in window_results)
    correct = sum(bool(result["entry_correct"]) for result in window_results)
    total_cost = sum(
        quantity * price + fee
        for result in window_results
        for _, quantity, price, fee, _ in result["fills"]  # type: ignore[union-attr]
    )
    print(f"ticks={len(ticks)} completed_windows={len(completed)}")
    print(
        f"traded_windows={len(window_results)} wins={wins} losses={losses} "
        f"window_win_rate={(Decimal(wins) / Decimal(len(window_results)) * 100 if window_results else Decimal('0')):.2f}%"
    )
    print(
        f"entry_direction_accuracy={(Decimal(correct) / Decimal(len(window_results)) * 100 if window_results else Decimal('0')):.2f}% "
        f"stopped_windows={stops}"
    )
    print(
        f"capital_turnover={total_cost:.4f} net_pnl={cumulative:.4f} "
        f"roi_on_turnover={(cumulative / total_cost * 100 if total_cost else Decimal('0')):.2f}% "
        f"max_drawdown={max_drawdown:.4f}"
    )
    for result in window_results:
        fills_text = ",".join(
            f"{role}:{side} {quantity}@{price}"
            for side, quantity, price, _, role in result["fills"]  # type: ignore[union-attr]
        )
        print(
            f"{result['slug']} winner={result['winner']} pnl={result['pnl']:.4f} "
            f"fills=[{fills_text}]"
        )


if __name__ == "__main__":
    main()
