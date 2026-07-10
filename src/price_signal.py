from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

import requests


PRICE_PATTERN = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)(\s*[kKmM])?")


@dataclass(frozen=True)
class SpotPrice:
    symbol: str
    price: Decimal
    source: str


@dataclass(frozen=True)
class DirectionSignal:
    outcome: str
    reason: str


class SpotPriceClient:
    def __init__(self, source: str, timeout: int = 20) -> None:
        self.source = source.upper()
        self.timeout = timeout

    def btc_usd(self) -> SpotPrice:
        if self.source == "BINANCE":
            return self._binance_btc_usdt()
        if self.source == "COINBASE":
            return self._coinbase_btc_usd()
        if self.source == "COINGECKO":
            return self._coingecko_btc_usd()
        raise ValueError("SPOT_PRICE_SOURCE must be BINANCE, COINBASE, or COINGECKO")

    def _binance_btc_usdt(self) -> SpotPrice:
        response = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return SpotPrice(symbol="BTC/USD", price=Decimal(str(response.json()["price"])), source="BINANCE")

    def _coinbase_btc_usd(self) -> SpotPrice:
        response = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=self.timeout)
        response.raise_for_status()
        amount = response.json()["data"]["amount"]
        return SpotPrice(symbol="BTC/USD", price=Decimal(str(amount)), source="COINBASE")

    def _coingecko_btc_usd(self) -> SpotPrice:
        response = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        price = response.json()["bitcoin"]["usd"]
        return SpotPrice(symbol="BTC/USD", price=Decimal(str(price)), source="COINGECKO")


def extract_price_threshold(text: str) -> Decimal | None:
    match = PRICE_PATTERN.search(text)
    if match is None:
        return None

    value = Decimal(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").strip().lower()
    if suffix == "k":
        value *= Decimal("1000")
    elif suffix == "m":
        value *= Decimal("1000000")
    return value


def build_threshold_signal(
    question: str,
    spot_price: SpotPrice,
    buffer_bps: Decimal,
) -> DirectionSignal | None:
    threshold = extract_price_threshold(question)
    if threshold is None:
        return None
    question_lower = question.lower()

    buffer = threshold * buffer_bps / Decimal("10000")
    upper_trigger = threshold + buffer
    lower_trigger = threshold - buffer

    if _is_downside_threshold(question_lower):
        if spot_price.price <= lower_trigger:
            return DirectionSignal(
                outcome="YES",
                reason=f"{spot_price.source} BTC/USD {spot_price.price} <= downside threshold {threshold} - buffer {buffer}",
            )
        if spot_price.price >= upper_trigger:
            return DirectionSignal(
                outcome="NO",
                reason=f"{spot_price.source} BTC/USD {spot_price.price} >= downside threshold {threshold} + buffer {buffer}",
            )
        return None

    if _is_upside_threshold(question_lower) and spot_price.price >= upper_trigger:
        return DirectionSignal(
            outcome="YES",
            reason=f"{spot_price.source} BTC/USD {spot_price.price} >= upside threshold {threshold} + buffer {buffer}",
        )
    if _is_upside_threshold(question_lower) and spot_price.price <= lower_trigger:
        return DirectionSignal(
            outcome="NO",
            reason=f"{spot_price.source} BTC/USD {spot_price.price} <= upside threshold {threshold} - buffer {buffer}",
        )
    return None


def _is_downside_threshold(question: str) -> bool:
    return any(phrase in question for phrase in ("dip to", "drop to", "fall to", "below", "under", "lower than"))


def _is_upside_threshold(question: str) -> bool:
    return any(phrase in question for phrase in ("reach", "hit", "above", "over", "higher than", "at least"))
