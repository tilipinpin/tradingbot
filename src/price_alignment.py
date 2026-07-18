from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import requests


@dataclass(frozen=True)
class PriceToBeat:
    open_price: Decimal
    close_price: Decimal | None
    completed: bool
    incomplete: bool
    timestamp_ms: int | None


class PolymarketPriceToBeatClient:
    URL = "https://polymarket.com/api/crypto/crypto-price"

    def __init__(self, timeout: int = 5, proxy_url: str | None = None) -> None:
        self.timeout = timeout
        self.proxies = (
            {"http": proxy_url, "https": proxy_url}
            if proxy_url
            else None
        )

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime,
        symbol: str = "BTC",
        variant: str = "fiveminute",
    ) -> PriceToBeat:
        response = requests.get(
            self.URL,
            params={
                "symbol": symbol,
                "eventStartTime": _utc_iso(start_time),
                "variant": variant,
                "endDate": _utc_iso(end_time),
            },
            headers={
                "Accept": "application/json",
                "Referer": "https://polymarket.com/",
                "User-Agent": "PolymarketTradingBot/price-alignment",
            },
            proxies=self.proxies,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        open_price = payload.get("openPrice")
        if open_price is None:
            raise ValueError("Polymarket price-to-beat response omitted openPrice")
        close_price = payload.get("closePrice")
        timestamp = payload.get("timestamp")
        return PriceToBeat(
            open_price=Decimal(str(open_price)),
            close_price=Decimal(str(close_price)) if close_price is not None else None,
            completed=bool(payload.get("completed")),
            incomplete=bool(payload.get("incomplete")),
            timestamp_ms=int(timestamp) if timestamp is not None else None,
        )


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
