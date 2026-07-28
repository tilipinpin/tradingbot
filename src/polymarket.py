from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import requests


GAMMA_API = "https://gamma-api.polymarket.com"


class OrderQuoteExpiredError(TimeoutError):
    """The final market quote became too old before the order POST began."""


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


@dataclass(frozen=True)
class OrderBookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBookSnapshot:
    token_id: str
    timestamp: str
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    minimum_order_size: Decimal

    @property
    def quote(self) -> OrderBookQuote:
        return OrderBookQuote(
            bid=max((level.price for level in self.bids), default=None),
            ask=min((level.price for level in self.asks), default=None),
        )


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
        return self.quotes((token_id,))[0]

    def quotes(self, token_ids: tuple[str, ...]) -> tuple[OrderBookQuote, ...]:
        if not token_ids:
            return ()
        response = requests.post(
            f"{self.host}/prices",
            json=[
                {"token_id": token_id, "side": side}
                for token_id in token_ids
                for side in ("BUY", "SELL")
            ],
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        quotes: list[OrderBookQuote] = []
        for token_id in token_ids:
            prices = payload.get(token_id, {})
            bid = prices.get("BUY")
            ask = prices.get("SELL")
            quotes.append(
                OrderBookQuote(
                    bid=Decimal(str(bid)) if bid is not None else None,
                    ask=Decimal(str(ask)) if ask is not None else None,
                )
            )
        return tuple(quotes)

    def books(self, token_ids: tuple[str, ...]) -> tuple[OrderBookSnapshot, ...]:
        if not token_ids:
            return ()
        response = requests.post(
            f"{self.host}/books",
            json=[{"token_id": token_id} for token_id in token_ids],
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("CLOB /books response must be a list")
        books = {
            book.token_id: book
            for item in payload
            if isinstance(item, dict)
            if (book := _order_book_from_payload(item)) is not None
        }
        missing = [token_id for token_id in token_ids if token_id not in books]
        if missing:
            raise LookupError(f"CLOB /books response omitted token(s): {', '.join(missing)}")
        return tuple(books[token_id] for token_id in token_ids)


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
        self._client_v2 = ClobClient.__module__.startswith("py_clob_client_v2")

        self.client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder_address,
        )
        creds = _create_or_derive_creds(self.client)
        super().__init__(host)
        if hasattr(self.client, "set_api_creds"):
            self.client.set_api_creds(creds)
        else:
            self.client = ClobClient(
                host,
                key=private_key,
                chain_id=chain_id,
                creds=creds,
                signature_type=signature_type,
                funder=funder_address,
            )

    def quote(self, token_id: str) -> OrderBookQuote:
        book = self.client.get_order_book(token_id)
        bids = getattr(book, "bids", None) or book.get("bids", [])
        asks = getattr(book, "asks", None) or book.get("asks", [])
        bid_prices = [_book_level_price(level) for level in bids]
        ask_prices = [_book_level_price(level) for level in asks]
        best_bid = max(bid_prices) if bid_prices else None
        best_ask = min(ask_prices) if ask_prices else None
        return OrderBookQuote(bid=best_bid, ask=best_ask)

    def prewarm_order_submission(self) -> None:
        """Populate SDK metadata that would otherwise delay the first signed order."""
        resolver = getattr(self.client, "_ClobClient__resolve_version", None)
        if resolver is not None:
            resolver()
        elif hasattr(self.client, "get_version"):
            self.client.get_version()

    def conditional_balance(self, token_id: str, signature_type: int) -> Decimal:
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams
        except ImportError:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id,
            signature_type=signature_type,
        )
        self.client.update_balance_allowance(params)
        payload = self.client.get_balance_allowance(params)
        return Decimal(str(payload.get("balance") or "0")) / Decimal("1000000")

    def open_orders(self) -> list[Any]:
        return list(self.client.get_open_orders() or [])

    def buy_limit(
        self,
        token_id: str,
        price: Decimal,
        size: Decimal,
        tick_size: str,
        neg_risk: bool,
        order_type: str = "GTC",
        submit_not_after_monotonic: float | None = None,
    ) -> dict[str, Any]:
        OrderArgs, OrderType, PartialCreateOrderOptions, buy_side = _import_order_types()
        return self._post_limit(
            OrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            buy_side,
            token_id,
            price,
            size,
            tick_size,
            neg_risk,
            order_type,
            submit_not_after_monotonic,
        )

    def sell_limit(
        self,
        token_id: str,
        price: Decimal,
        size: Decimal,
        tick_size: str,
        neg_risk: bool,
        order_type: str = "GTC",
        submit_not_after_monotonic: float | None = None,
    ) -> dict[str, Any]:
        OrderArgs, OrderType, PartialCreateOrderOptions, sell_side = _import_sell_order_types()
        return self._post_limit(
            OrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            sell_side,
            token_id,
            price,
            size,
            tick_size,
            neg_risk,
            order_type,
            submit_not_after_monotonic,
        )

    def _post_limit(
        self,
        OrderArgs: Any,
        OrderType: Any,
        PartialCreateOrderOptions: Any,
        side: str,
        token_id: str,
        price: Decimal,
        size: Decimal,
        tick_size: str,
        neg_risk: bool,
        order_type: str,
        submit_not_after_monotonic: float | None,
    ) -> dict[str, Any]:
        selected_order_type = getattr(OrderType, order_type.upper())
        order_args = OrderArgs(
            token_id=token_id,
            price=float(price),
            size=float(size),
            side=side,
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)

        if getattr(self, "_client_v2", False) and submit_not_after_monotonic is None:
            return self.client.create_and_post_order(
                order_args,
                options=options,
                order_type=selected_order_type,
            )

        if (
            order_type.upper() == "GTC"
            and submit_not_after_monotonic is None
            and hasattr(self.client, "create_and_post_order")
        ):
            return self.client.create_and_post_order(order_args, options=options)

        signed_order = self.client.create_order(order_args, options=options)
        if (
            submit_not_after_monotonic is not None
            and time.monotonic() > submit_not_after_monotonic
        ):
            raise OrderQuoteExpiredError(
                "Final order-book quote expired before order submission"
            )
        return self.client.post_order(signed_order, selected_order_type)


def _import_clob_client() -> Any:
    try:
        from py_clob_client_v2 import ClobClient

        return ClobClient
    except ImportError:
        from py_clob_client.client import ClobClient

        return ClobClient


def _book_level_price(level: Any) -> Decimal:
    raw = level["price"] if isinstance(level, dict) else level.price
    return Decimal(str(raw))


def _order_book_from_payload(item: dict[str, Any]) -> OrderBookSnapshot | None:
    token_id = str(item.get("asset_id") or item.get("assetId") or "").strip()
    if not token_id:
        return None

    def levels(key: str, *, reverse: bool) -> tuple[OrderBookLevel, ...]:
        parsed = [
            OrderBookLevel(
                price=Decimal(str(level["price"])),
                size=Decimal(str(level["size"])),
            )
            for level in item.get(key, [])
            if isinstance(level, dict)
            and level.get("price") not in (None, "")
            and level.get("size") not in (None, "")
        ]
        return tuple(sorted(parsed, key=lambda level: level.price, reverse=reverse))

    return OrderBookSnapshot(
        token_id=token_id,
        timestamp=str(item.get("timestamp") or ""),
        bids=levels("bids", reverse=True),
        asks=levels("asks", reverse=False),
        minimum_order_size=_parse_decimal(item.get("min_order_size") or item.get("minOrderSize"), "0"),
    )


def _import_order_types() -> tuple[Any, Any, Any, str]:
    try:
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY

        return OrderArgs, OrderType, PartialCreateOrderOptions, BUY
    except ImportError:
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY

        return OrderArgs, OrderType, PartialCreateOrderOptions, BUY


def _import_sell_order_types() -> tuple[Any, Any, Any, str]:
    try:
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import SELL

        return OrderArgs, OrderType, PartialCreateOrderOptions, SELL
    except ImportError:
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import SELL

        return OrderArgs, OrderType, PartialCreateOrderOptions, SELL


def _create_or_derive_creds(client: Any) -> Any:
    for method_name in (
        "derive_api_creds",
        "derive_api_key",
        "create_or_derive_api_creds",
        "create_or_derive_api_key",
    ):
        method = getattr(client, method_name, None)
        if method is not None:
            return method()
    raise AttributeError("CLOB client does not expose an API credential derivation method")
