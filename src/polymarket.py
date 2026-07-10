from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import requests


GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass(frozen=True)
class Market:
    question: str
    slug: str
    condition_id: str
    token_ids: tuple[str, str]
    minimum_tick_size: str
    neg_risk: bool
    liquidity: Decimal
    outcomes: tuple[str, str]
    event_start_time: datetime | None
    end_time: datetime | None


@dataclass(frozen=True)
class Event:
    title: str
    slug: str
    markets: tuple[Market, ...]


@dataclass(frozen=True)
class OrderBookQuote:
    bid: Decimal | None
    ask: Decimal | None


def _parse_token_ids(raw: Any) -> tuple[str, str] | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    return str(raw[0]), str(raw[1])


def _parse_outcomes(raw: Any) -> tuple[str, str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return "Yes", "No"
    if isinstance(raw, list) and len(raw) >= 2:
        return str(raw[0]), str(raw[1])
    return "Yes", "No"


def _parse_decimal(raw: Any, default: str = "0") -> Decimal:
    if raw in (None, ""):
        return Decimal(default)
    return Decimal(str(raw))


def _parse_datetime(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _market_from_payload(item: dict[str, Any]) -> Market | None:
    token_ids = _parse_token_ids(item.get("clobTokenIds"))
    condition_id = item.get("conditionId") or item.get("condition_id")
    if token_ids is None or not condition_id:
        return None

    return Market(
        question=str(item.get("question") or ""),
        slug=str(item.get("slug") or ""),
        condition_id=str(condition_id),
        token_ids=token_ids,
        minimum_tick_size=str(item.get("minimum_tick_size") or item.get("minimumTickSize") or "0.01"),
        neg_risk=bool(item.get("neg_risk") or item.get("negRisk") or False),
        liquidity=_parse_decimal(item.get("liquidityClob") or item.get("liquidity")),
        outcomes=_parse_outcomes(item.get("outcomes")),
        event_start_time=_parse_datetime(item.get("eventStartTime")),
        end_time=_parse_datetime(item.get("endDate") or item.get("endDateIso")),
    )


class GammaClient:
    def __init__(self, base_url: str = GAMMA_API, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def active_markets(self, limit: int = 50) -> list[Market]:
        response = requests.get(
            f"{self.base_url}/markets",
            params={"active": "true", "closed": "false", "limit": limit},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return [market for item in response.json() if (market := _market_from_payload(item))]

    def market_by_slug(self, slug: str) -> Market:
        response = requests.get(
            f"{self.base_url}/markets",
            params={"slug": slug, "limit": 1},
            timeout=self.timeout,
        )
        response.raise_for_status()
        markets = [market for item in response.json() if (market := _market_from_payload(item))]
        if not markets:
            raise LookupError(f"No Polymarket market found for slug: {slug}")
        return markets[0]

    def event_by_slug(self, slug: str) -> Event:
        response = requests.get(
            f"{self.base_url}/events",
            params={"slug": slug, "limit": 1},
            timeout=self.timeout,
        )
        response.raise_for_status()
        events = response.json()
        if not events:
            raise LookupError(f"No Polymarket event found for slug: {slug}")

        item = events[0]
        markets = tuple(
            market
            for payload in item.get("markets", [])
            if isinstance(payload, dict)
            if (market := _market_from_payload(payload))
        )
        return Event(
            title=str(item.get("title") or ""),
            slug=str(item.get("slug") or ""),
            markets=markets,
        )


def choose_btc_markets(
    markets: list[Market],
    keywords: tuple[str, ...],
    direction_keywords: tuple[str, ...],
) -> list[Market]:
    selected: list[Market] = []
    for market in markets:
        haystack = f"{market.question} {market.slug}".lower()
        has_asset = any(keyword in haystack for keyword in keywords)
        has_direction = any(keyword in haystack for keyword in direction_keywords)
        if has_asset and has_direction:
            selected.append(market)
    return selected


def rank_markets_by_liquidity(markets: list[Market]) -> list[Market]:
    return sorted(markets, key=lambda market: market.liquidity, reverse=True)


def filter_markets_by_liquidity(markets: list[Market], min_liquidity: Decimal) -> list[Market]:
    return [market for market in markets if market.liquidity >= min_liquidity]


class ClobDataClient:
    def __init__(self, host: str, timeout: int = 20) -> None:
        self.host = host.rstrip("/")
        self.timeout = timeout

    def quote(self, token_id: str) -> OrderBookQuote:
        bid = self._price(token_id, "SELL")
        ask = self._price(token_id, "BUY")
        return OrderBookQuote(bid=bid, ask=ask)

    def _price(self, token_id: str, side: str) -> Decimal | None:
        response = requests.get(
            f"{self.host}/price",
            params={"token_id": token_id, "side": side},
            timeout=self.timeout,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        price = payload.get("price")
        return Decimal(str(price)) if price is not None else None


class ClobTradingClient(ClobDataClient):
    def __init__(
        self,
        host: str,
        chain_id: int,
        private_key: str,
        funder_address: str,
        signature_type: int,
    ) -> None:
        ClobClient = _import_clob_client()

        temp_client = ClobClient(host, key=private_key, chain_id=chain_id)
        creds = _create_or_derive_creds(temp_client)
        super().__init__(host)
        self.client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            creds=creds,
            signature_type=signature_type,
            funder=funder_address,
        )
        if hasattr(self.client, "set_api_creds"):
            self.client.set_api_creds(creds)

    def quote(self, token_id: str) -> OrderBookQuote:
        book = self.client.get_order_book(token_id)
        bids = getattr(book, "bids", None) or book.get("bids", [])
        asks = getattr(book, "asks", None) or book.get("asks", [])
        best_bid = Decimal(str(bids[0]["price"])) if bids else None
        best_ask = Decimal(str(asks[0]["price"])) if asks else None
        return OrderBookQuote(bid=best_bid, ask=best_ask)

    def buy_limit(
        self,
        token_id: str,
        price: Decimal,
        size: Decimal,
        tick_size: str,
        neg_risk: bool,
    ) -> dict[str, Any]:
        OrderArgs, OrderType, PartialCreateOrderOptions, buy_side = _import_order_types()
        order_args = OrderArgs(
            token_id=token_id,
            price=float(price),
            size=float(size),
            side=buy_side,
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)

        if hasattr(self.client, "create_and_post_order"):
            return self.client.create_and_post_order(order_args, options=options, order_type=OrderType.GTC)

        signed_order = self.client.create_order(order_args, options=options)
        return self.client.post_order(signed_order, OrderType.GTC)


def _import_clob_client() -> Any:
    try:
        from py_clob_client.client import ClobClient

        return ClobClient
    except ImportError:
        from py_clob_client_v2 import ClobClient

        return ClobClient


def _import_order_types() -> tuple[Any, Any, Any, str]:
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY

        return OrderArgs, OrderType, PartialCreateOrderOptions, BUY
    except ImportError:
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY

        return OrderArgs, OrderType, PartialCreateOrderOptions, BUY


def _create_or_derive_creds(client: Any) -> Any:
    for method_name in ("create_or_derive_api_creds", "create_or_derive_api_key"):
        method = getattr(client, method_name, None)
        if method is not None:
            return method()
    raise AttributeError("CLOB client does not expose an API credential derivation method")
