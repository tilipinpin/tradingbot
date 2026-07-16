from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from src.polymarket import OrderBookQuote


@dataclass(frozen=True)
class MarketSnapshot:
    observed_at: str
    observed_ts: int
    slug: str
    market_start_ts: int
    market_end_ts: int
    seconds_left: int
    spot: str
    start_spot: str
    spot_source: str
    probability_up: str
    up_bid: str | None
    up_ask: str | None
    down_bid: str | None
    down_ask: str | None


def _value(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def build_snapshot(
    *,
    observed_at: str,
    observed_ts: int,
    slug: str,
    market_start_ts: int,
    market_end_ts: int,
    seconds_left: Decimal,
    spot: Decimal,
    start_spot: Decimal,
    spot_source: str,
    probability_up: Decimal,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
) -> MarketSnapshot:
    return MarketSnapshot(
        observed_at=observed_at,
        observed_ts=observed_ts,
        slug=slug,
        market_start_ts=market_start_ts,
        market_end_ts=market_end_ts,
        seconds_left=max(0, int(seconds_left)),
        spot=str(spot),
        start_spot=str(start_spot),
        spot_source=spot_source,
        probability_up=str(probability_up),
        up_bid=_value(up_quote.bid if up_quote else None),
        up_ask=_value(up_quote.ask if up_quote else None),
        down_bid=_value(down_quote.bid if down_quote else None),
        down_ask=_value(down_quote.ask if down_quote else None),
    )


class JsonlSnapshotWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, snapshot: MarketSnapshot) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(snapshot), separators=(",", ":")) + "\n")
