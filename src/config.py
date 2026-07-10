from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

from dotenv import load_dotenv


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _decimal(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default))


def _slug(value: str | None, path_marker: str) -> str | None:
    if not value:
        return None

    parsed = urlparse(value.strip())
    if not parsed.scheme:
        return value.strip().strip("/")

    parts = [part for part in parsed.path.split("/") if part]
    if path_marker in parts:
        index = parts.index(path_marker)
        if index + 1 < len(parts):
            return parts[index + 1]
    return parts[-1] if parts else None


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    live_trading: bool
    event_slug: str | None
    market_slug: str | None
    trade_outcome: str
    spot_price_source: str
    manual_btc_price: Decimal | None
    threshold_buffer_bps: Decimal
    max_price: Decimal
    order_size: Decimal
    max_daily_usd: Decimal
    min_market_liquidity: Decimal
    max_markets_per_run: int
    market_query: tuple[str, ...]
    market_direction_query: tuple[str, ...]
    market_limit: int
    clob_host: str
    chain_id: int
    signature_type: int
    private_key: str | None
    funder_address: str | None


def load_settings() -> Settings:
    load_dotenv()

    outcome = os.getenv("TRADE_OUTCOME", "YES").strip().upper()
    if outcome not in {"YES", "NO", "AUTO"}:
        raise ValueError("TRADE_OUTCOME must be YES, NO, or AUTO")

    query = tuple(
        token.lower()
        for token in os.getenv("MARKET_QUERY", "bitcoin btc").replace(",", " ").split()
        if token.strip()
    )
    direction_query = tuple(
        token.lower()
        for token in os.getenv("MARKET_DIRECTION_QUERY", "up down higher lower above below").replace(",", " ").split()
        if token.strip()
    )

    return Settings(
        dry_run=_bool("DRY_RUN", True),
        live_trading=_bool("LIVE_TRADING", False),
        event_slug=_slug(os.getenv("POLYMARKET_EVENT_SLUG"), "event"),
        market_slug=_slug(os.getenv("POLYMARKET_MARKET_SLUG"), "market"),
        trade_outcome=outcome,
        spot_price_source=os.getenv("SPOT_PRICE_SOURCE", "COINGECKO"),
        manual_btc_price=_decimal("MANUAL_BTC_PRICE", "0") if os.getenv("MANUAL_BTC_PRICE") else None,
        threshold_buffer_bps=_decimal("THRESHOLD_BUFFER_BPS", "25"),
        max_price=_decimal("MAX_PRICE", "0.55"),
        order_size=_decimal("ORDER_SIZE", "5"),
        max_daily_usd=_decimal("MAX_DAILY_USD", "25"),
        min_market_liquidity=_decimal("MIN_MARKET_LIQUIDITY", "0"),
        max_markets_per_run=int(os.getenv("MAX_MARKETS_PER_RUN", "3")),
        market_query=query,
        market_direction_query=direction_query,
        market_limit=int(os.getenv("MARKET_LIMIT", "50")),
        clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
        chain_id=int(os.getenv("CHAIN_ID", "137")),
        signature_type=int(os.getenv("SIGNATURE_TYPE", "0")),
        private_key=os.getenv("PRIVATE_KEY") or None,
        funder_address=os.getenv("FUNDER_ADDRESS") or None,
    )
