import json
from decimal import Decimal

import pytest

from src.crypto_resolution import (
    CryptoResolutionMode,
    detect_crypto_resolution_mode,
    has_official_btc_5m_twap_rule,
    resolve_btc_5m_twap,
)
from src.price_signal import PolymarketChainlinkTwapStream


def test_auto_detects_official_60_second_twap_rule() -> None:
    rules = "Resolution uses https://data.chain.link/streams/btc-usd-twap-60s-streams."

    assert detect_crypto_resolution_mode(rules) is CryptoResolutionMode.TWAP_60


def test_official_btc_5m_rule_requires_exact_chainlink_source() -> None:
    official = (
        "The resolution source is information from Chainlink, specifically the "
        "BTC/USD TWAP data stream at "
        "https://data.chain.link/streams/btc-usd-twap-60s-streams."
    )

    assert has_official_btc_5m_twap_rule(official)
    assert not has_official_btc_5m_twap_rule("BTC/USD Chainlink spot price")
    assert not has_official_btc_5m_twap_rule("Binance BTC/USD 60-second TWAP")


def test_official_btc_5m_winner_rule_including_equality() -> None:
    opening = Decimal("79122.16778388181")

    assert resolve_btc_5m_twap(opening, Decimal("79135.94853121213")) == "UP"
    assert resolve_btc_5m_twap(opening, opening) == "UP"
    assert resolve_btc_5m_twap(opening, Decimal("79118.25910090169")) == "DOWN"

    with pytest.raises(ValueError, match="must be positive"):
        resolve_btc_5m_twap(opening, Decimal("0"))


def test_auto_keeps_legacy_without_authoritative_twap_marker() -> None:
    assert detect_crypto_resolution_mode("BTC/USD Chainlink price feed") is CryptoResolutionMode.LEGACY


def test_explicit_mode_overrides_rule_detection() -> None:
    assert (
        detect_crypto_resolution_mode("60-second TWAP", "legacy")
        is CryptoResolutionMode.LEGACY
    )


def test_twap_parser_uses_exact_e18_value_not_display_value() -> None:
    message = json.dumps(
        {
            "topic": "crypto_prices_twap_sixty",
            "type": "update",
            "timestamp": 1785859200100,
            "payload": {
                "symbol": "btc/usd",
                "value": 65000.5,
                "full_accuracy_value": "65000500000000000000123",
                "timestamp": 1785859200000,
                "window_s": 60,
            },
        }
    )

    price = PolymarketChainlinkTwapStream.parse_message(message)

    assert price is not None
    assert price.price == Decimal("65000.500000000000000123")
    assert price.observed_at_ms == 1785859200000
    assert price.source == "POLYMARKET_CHAINLINK_TWAP_60"


@pytest.mark.parametrize("field,value", [("window_s", 30), ("symbol", "eth/usd")])
def test_twap_parser_rejects_wrong_feed(field: str, value: object) -> None:
    payload = {
        "symbol": "btc/usd",
        "full_accuracy_value": "65000000000000000000000",
        "timestamp": 1785859200000,
        "window_s": 60,
    }
    payload[field] = value
    message = json.dumps(
        {"topic": "crypto_prices_twap_sixty", "type": "update", "payload": payload}
    )

    assert PolymarketChainlinkTwapStream.parse_message(message) is None


def test_twap_parser_requires_exact_value() -> None:
    message = json.dumps(
        {
            "topic": "crypto_prices_twap_sixty",
            "type": "update",
            "payload": {
                "symbol": "btc/usd",
                "value": 65000.5,
                "timestamp": 1785859200000,
                "window_s": 60,
            },
        }
    )

    with pytest.raises(ValueError, match="full_accuracy_value"):
        PolymarketChainlinkTwapStream.parse_message(message)
