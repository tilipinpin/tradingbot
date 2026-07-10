from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.polymarket import Market, OrderBookQuote


@dataclass(frozen=True)
class TradeIntent:
    market: Market
    outcome: str
    token_id: str
    price: Decimal
    size: Decimal
    reason: str


def build_buy_intent(
    market: Market,
    outcome: str,
    quote: OrderBookQuote,
    max_price: Decimal,
    size: Decimal,
    signal_reason: str | None = None,
) -> TradeIntent | None:
    if quote.ask is None:
        return None
    if quote.ask > max_price:
        return None

    token_id = market.token_ids[0] if outcome == "YES" else market.token_ids[1]
    return TradeIntent(
        market=market,
        outcome=outcome,
        token_id=token_id,
        price=quote.ask,
        size=size,
        reason=f"best ask {quote.ask} <= max price {max_price}; {signal_reason or 'manual outcome'}",
    )
