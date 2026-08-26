from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import json
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal

import aiohttp
import requests
import certifi
from requests import RequestException


PRICE_PATTERN = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)(\s*[kKmM])?")


@dataclass(frozen=True)
class SpotPrice:
    symbol: str
    price: Decimal
    source: str
    observed_at: int | None = None
    observed_at_ms: int | None = None


@dataclass(frozen=True)
class DirectionSignal:
    outcome: str
    reason: str


class SpotPriceClient:
    EXCHANGE_SOURCES = ("BINANCE", "COINBASE", "KRAKEN", "COINGECKO")
    SOURCES = ("POLYMARKET_CHAINLINK", *EXCHANGE_SOURCES)
    CHAINLINK_API_URL = "https://api.dataengine.chain.link"

    def __init__(self, source: str, timeout: int = 20, ws_proxy: str | None = None) -> None:
        self.source = source.upper()
        self.timeout = timeout
        self.ws_proxy = ws_proxy
        self._polymarket_stream: PolymarketChainlinkStream | None = None
        self._polymarket_twap_stream: PolymarketChainlinkTwapStream | None = None
        self._polymarket_failures = 0

    def btc_usd(self) -> SpotPrice:
        if self.source == "CHAINLINK":
            return self._chainlink_btc_usd()
        if self.source == "AUTO":
            sources = self.SOURCES
        elif self.source == "POLYMARKET_CHAINLINK":
            sources = (self.source,)
        else:
            sources = (self.source, *[source for source in self.EXCHANGE_SOURCES if source != self.source])
        errors: list[str] = []
        for source in sources:
            try:
                price = self._btc_usd_from_source(source)
                if source == "POLYMARKET_CHAINLINK":
                    self._polymarket_failures = 0
                return price
            except (KeyError, RequestException, RuntimeError, ValueError) as exc:
                errors.append(f"{source}: {exc}")
                if source == "POLYMARKET_CHAINLINK":
                    self._polymarket_failures += 1
                    if self._polymarket_failures >= 3:
                        stream = self._polymarket_stream
                        self._polymarket_stream = None
                        self._polymarket_failures = 0
                        if stream is not None:
                            stream.close()
        raise RuntimeError(f"All BTC/USD price sources failed: {'; '.join(errors)}")

    def polymarket_chainlink_price_near(
        self,
        timestamp_ms: int,
        max_distance_ms: int,
    ) -> SpotPrice:
        if self._polymarket_stream is None:
            raise RuntimeError("Polymarket Chainlink stream has not started")
        return self._polymarket_stream.price_near(timestamp_ms, max_distance_ms)

    def polymarket_chainlink_price_at_or_after(
        self,
        timestamp_ms: int,
        max_delay_ms: int,
    ) -> SpotPrice:
        if self._polymarket_stream is None:
            raise RuntimeError("Polymarket Chainlink stream has not started")
        return self._polymarket_stream.price_at_or_after(timestamp_ms, max_delay_ms)

    def polymarket_chainlink_twap(self) -> SpotPrice:
        if self._polymarket_twap_stream is None:
            self._polymarket_twap_stream = PolymarketChainlinkTwapStream(
                timeout=self.timeout,
                proxy_url=self.ws_proxy,
            )
        return self._polymarket_twap_stream.btc_usd()

    def warm_polymarket_chainlink_twap(self) -> None:
        if self._polymarket_twap_stream is None:
            self._polymarket_twap_stream = PolymarketChainlinkTwapStream(
                timeout=self.timeout,
                proxy_url=self.ws_proxy,
            )
        self._polymarket_twap_stream.start()

    def warm_polymarket_chainlink_spot(self) -> None:
        if self._polymarket_stream is None:
            self._polymarket_stream = PolymarketChainlinkStream(
                timeout=self.timeout,
                proxy_url=self.ws_proxy,
            )
        self._polymarket_stream.start()

    def polymarket_chainlink_twap_near(
        self,
        timestamp_ms: int,
        max_distance_ms: int,
    ) -> SpotPrice:
        if self._polymarket_twap_stream is None:
            raise RuntimeError("Polymarket Chainlink 60-second TWAP stream has not started")
        return self._polymarket_twap_stream.price_near(timestamp_ms, max_distance_ms)

    def _btc_usd_from_source(self, source: str) -> SpotPrice:
        if source == "POLYMARKET_CHAINLINK":
            if self._polymarket_stream is None:
                self._polymarket_stream = PolymarketChainlinkStream(timeout=self.timeout, proxy_url=self.ws_proxy)
            return self._polymarket_stream.btc_usd()
        if source == "BINANCE":
            return self._binance_btc_usdt()
        if source == "COINBASE":
            return self._coinbase_btc_usd()
        if source == "COINGECKO":
            return self._coingecko_btc_usd()
        if source == "KRAKEN":
            return self._kraken_btc_usd()
        raise ValueError(
            "SPOT_PRICE_SOURCE must be AUTO, POLYMARKET_CHAINLINK, CHAINLINK, "
            "BINANCE, COINBASE, KRAKEN, or COINGECKO"
        )

    def _chainlink_btc_usd(self) -> SpotPrice:
        api_key = os.getenv("CHAINLINK_DATA_STREAMS_API_KEY")
        api_secret = os.getenv("CHAINLINK_DATA_STREAMS_API_SECRET")
        feed_id = os.getenv("CHAINLINK_BTC_USD_FEED_ID")
        if not api_key or not api_secret or not feed_id:
            raise RuntimeError(
                "CHAINLINK price source requires CHAINLINK_DATA_STREAMS_API_KEY, "
                "CHAINLINK_DATA_STREAMS_API_SECRET, and CHAINLINK_BTC_USD_FEED_ID"
            )

        path = f"/api/v1/reports/latest?feedID={feed_id}"
        timestamp_ms = str(int(time.time() * 1000))
        body_hash = hashlib.sha256(b"").hexdigest()
        string_to_sign = f"GET {path} {body_hash} {api_key} {timestamp_ms}"
        signature = hmac.new(api_secret.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()
        response = requests.get(
            f"{os.getenv('CHAINLINK_DATA_STREAMS_API_URL', self.CHAINLINK_API_URL)}{path}",
            headers={
                "Authorization": api_key,
                "X-Authorization-Timestamp": timestamp_ms,
                "X-Authorization-Signature-SHA256": signature,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        report = response.json()["report"]
        price = decode_chainlink_v3_price(report["fullReport"], feed_id)
        observed_at = int(report["observationsTimestamp"])
        return SpotPrice(
            symbol="BTC/USD",
            price=price,
            source="CHAINLINK",
            observed_at=observed_at,
            observed_at_ms=observed_at * 1000,
        )

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

    def _kraken_btc_usd(self) -> SpotPrice:
        response = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": "XBTUSD"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"Kraken ticker error: {payload['error']}")
        result = payload["result"]
        ticker = next(iter(result.values()))
        return SpotPrice(symbol="BTC/USD", price=Decimal(str(ticker["c"][0])), source="KRAKEN")

    def _coingecko_btc_usd(self) -> SpotPrice:
        response = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        price = response.json()["bitcoin"]["usd"]
        return SpotPrice(symbol="BTC/USD", price=Decimal(str(price)), source="COINGECKO")


def decode_chainlink_v3_price(full_report: str, expected_feed_id: str) -> Decimal:
    raw = bytes.fromhex(full_report.removeprefix("0x"))
    if len(raw) < 7 * 32:
        raise ValueError("Chainlink fullReport is too short")

    report_offset = int.from_bytes(raw[3 * 32 : 4 * 32], "big")
    if report_offset + 32 > len(raw):
        raise ValueError("Chainlink fullReport contains an invalid report offset")
    report_length = int.from_bytes(raw[report_offset : report_offset + 32], "big")
    payload_start = report_offset + 32
    payload_end = payload_start + report_length
    if report_length < 9 * 32 or payload_end > len(raw):
        raise ValueError("Chainlink v3 report payload is incomplete")

    payload = raw[payload_start:payload_end]
    actual_feed_id = "0x" + payload[:32].hex()
    if actual_feed_id.lower() != expected_feed_id.lower():
        raise ValueError("Chainlink report feed ID does not match configured BTC/USD feed")

    price_word = payload[6 * 32 : 7 * 32]
    price_raw = int.from_bytes(price_word, "big", signed=True)
    if price_raw <= 0:
        raise ValueError("Chainlink BTC/USD report contains a non-positive price")
    return Decimal(price_raw) / Decimal(10**18)


class PolymarketChainlinkStream:
    URL = "wss://ws-live-data.polymarket.com"
    SUBSCRIPTION = {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": '{"symbol":"btc/usd"}',
            }
        ],
    }

    def __init__(self, timeout: int = 5, proxy_url: str | None = None, max_stale_seconds: int = 15) -> None:
        self.timeout = timeout
        self.proxy_url = proxy_url
        self.max_stale_seconds = max_stale_seconds
        self._latest: SpotPrice | None = None
        self._history: deque[SpotPrice] = deque(maxlen=4096)
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._started = False
        self._lock = threading.Lock()
        self._last_error: str | None = None

    @staticmethod
    def parse_message(message: str) -> SpotPrice | None:
        payload = json.loads(message)
        if payload.get("topic") != "crypto_prices_chainlink" or payload.get("type") != "update":
            return None
        price = payload.get("payload") or {}
        if str(price.get("symbol", "")).lower() != "btc/usd":
            return None
        timestamp_ms = int(price["timestamp"])
        return SpotPrice(
            symbol="BTC/USD",
            price=Decimal(str(price["value"])),
            source="POLYMARKET_CHAINLINK",
            observed_at=timestamp_ms // 1000,
            observed_at_ms=timestamp_ms,
        )

    def btc_usd(self) -> SpotPrice:
        self.start()
        initial_stream_timeout = max(12, self.timeout)
        if not self._ready.wait(initial_stream_timeout):
            detail = f": {self._last_error}" if self._last_error else ""
            raise RuntimeError(f"Timed out waiting for Polymarket Chainlink BTC/USD stream{detail}")
        with self._lock:
            latest = self._latest
        assert latest is not None
        if latest.observed_at is None or int(time.time()) - latest.observed_at > self.max_stale_seconds:
            raise RuntimeError("Polymarket Chainlink BTC/USD stream is stale")
        return latest

    def start(self) -> None:
        with self._lock:
            if not self._started:
                self._started = True
                threading.Thread(target=self._run, name="polymarket-chainlink", daemon=True).start()

    def price_near(self, timestamp_ms: int, max_distance_ms: int) -> SpotPrice:
        if max_distance_ms < 0:
            raise ValueError("max_distance_ms must be non-negative")
        with self._lock:
            candidates = [price for price in self._history if price.observed_at_ms is not None]
        if not candidates:
            raise RuntimeError("Polymarket Chainlink boundary history is unavailable")
        nearest = min(
            candidates,
            key=lambda price: abs(int(price.observed_at_ms) - timestamp_ms),
        )
        distance_ms = abs(int(nearest.observed_at_ms) - timestamp_ms)
        if distance_ms > max_distance_ms:
            raise RuntimeError(
                f"Nearest Polymarket Chainlink sample is {distance_ms}ms from boundary"
            )
        return nearest

    def price_at_or_after(self, timestamp_ms: int, max_delay_ms: int) -> SpotPrice:
        """Return the first RTDS tick on or just after a window boundary.

        A pre-boundary tick must never become the provisional Price to Beat.
        The bounded delay also prevents a bot that started mid-window from
        treating a fresh but late tick as the opening strike.
        """
        if max_delay_ms < 0:
            raise ValueError("max_delay_ms must be non-negative")
        with self._lock:
            candidates = [
                price
                for price in self._history
                if price.observed_at_ms is not None
                and int(price.observed_at_ms) >= timestamp_ms
            ]
        if not candidates:
            raise RuntimeError("Polymarket Chainlink post-boundary sample is unavailable")
        first = min(candidates, key=lambda price: int(price.observed_at_ms))
        delay_ms = int(first.observed_at_ms) - timestamp_ms
        if delay_ms > max_delay_ms:
            raise RuntimeError(
                f"First Polymarket Chainlink sample is {delay_ms}ms after boundary"
            )
        return first

    def close(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        timeout = aiohttp.ClientTimeout(total=None, connect=self.timeout)
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(
                        self.URL,
                        proxy=self.proxy_url,
                        ssl=ssl_context,
                        heartbeat=None,
                    ) as connection:
                        await connection.send_json(self.SUBSCRIPTION)
                        await connection.send_str("PING")
                        last_ping_at = time.monotonic()
                        while not self._stop.is_set():
                            try:
                                message = await connection.receive(timeout=self.timeout)
                            except asyncio.TimeoutError:
                                message = None
                            if time.monotonic() - last_ping_at >= 5:
                                await connection.send_str("PING")
                                last_ping_at = time.monotonic()
                            if message is None:
                                with self._lock:
                                    latest = self._latest
                                if (
                                    latest is not None
                                    and latest.observed_at is not None
                                    and int(time.time()) - latest.observed_at
                                    > self.max_stale_seconds
                                ):
                                    raise RuntimeError(
                                        "Polymarket Chainlink BTC/USD stream stopped updating"
                                    )
                                continue
                            if message.type == aiohttp.WSMsgType.TEXT:
                                if not message.data:
                                    continue
                                price = self.parse_message(str(message.data))
                                if price is not None:
                                    with self._lock:
                                        self._latest = price
                                        self._history.append(price)
                                    self._ready.set()
                                continue
                            if message.type in {
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            }:
                                raise RuntimeError(
                                    f"Polymarket Chainlink WebSocket closed: {message.data}"
                                )
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
                await asyncio.sleep(1)


class PolymarketChainlinkTwapStream(PolymarketChainlinkStream):
    """Official RTDS BTC/USD 60-second TWAP using its exact E18 value."""

    SUBSCRIPTION = {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_twap_sixty",
                "type": "update",
                "filters": '{"symbol":"btc/usd"}',
            }
        ],
    }

    def __init__(self, timeout: int = 5, proxy_url: str | None = None, max_stale_seconds: int = 45) -> None:
        super().__init__(timeout=timeout, proxy_url=proxy_url, max_stale_seconds=max_stale_seconds)
        self._history = deque(maxlen=16384)

    @staticmethod
    def parse_message(message: str) -> SpotPrice | None:
        envelope = json.loads(message)
        if envelope.get("topic") != "crypto_prices_twap_sixty" or envelope.get("type") != "update":
            return None
        payload = envelope.get("payload") or {}
        if str(payload.get("symbol", "")).lower() != "btc/usd":
            return None
        if int(payload.get("window_s", 0)) != 60:
            return None
        exact = payload.get("full_accuracy_value")
        if exact is None:
            raise ValueError("Chainlink TWAP update omitted full_accuracy_value")
        price = Decimal(str(exact)) / Decimal(10**18)
        if price <= 0:
            raise ValueError("Chainlink TWAP update contains a non-positive price")
        timestamp_ms = int(payload["timestamp"])
        return SpotPrice(
            symbol="BTC/USD",
            price=price,
            source="POLYMARKET_CHAINLINK_TWAP_60",
            observed_at=timestamp_ms // 1000,
            observed_at_ms=timestamp_ms,
        )


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
