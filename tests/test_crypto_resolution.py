import json
from decimal import Decimal

import pytest

from src.crypto_resolution import CryptoResolutionMode, detect_crypto_resolution_mode
from src.price_signal import PolymarketChainlinkTwapStream


def test_auto_detects_official_30_second_twap_rule() -> None:
    rules = "Resolution uses the Chainlink 30-second time-weighted average (TWAP)."

    assert detect_crypto_resolution_mode(rules) is CryptoResolutionMode.TWAP_30


def test_auto_keeps_legacy_without_authoritative_twap_marker() -> None:
    assert detect_crypto_resolution_mode("BTC/USD Chainlink price feed") is CryptoResolutionMode.LEGACY


def test_explicit_mode_overrides_rule_detection() -> None:
    assert (
        detect_crypto_resolution_mode("30-second TWAP", "legacy")
        is CryptoResolutionMode.LEGACY
    )


def test_twap_parser_uses_exact_e18_value_not_display_value() -> None:
    message = json.dumps(
        {
            "topic": "crypto_prices_twap_thirty",
            "type": "update",
            "timestamp": 1785859200100,
            "payload": {
                "symbol": "btc/usd",
                "value": 65000.5,
                "full_accuracy_value": "65000500000000000000123",
                "timestamp": 1785859200000,
                "window_s": 30,
            },
        }
    )

    price = PolymarketChainlinkTwapStream.parse_message(message)

    assert price is not None
    assert price.price == Decimal("65000.500000000000000123")
    assert price.observed_at_ms == 1785859200000
    assert price.source == "POLYMARKET_CHAINLINK_TWAP_30"


@pytest.mark.parametrize("field,value", [("window_s", 60), ("symbol", "eth/usd")])
def test_twap_parser_rejects_wrong_feed(field: str, value: object) -> None:
    payload = {
        "symbol": "btc/usd",
        "full_accuracy_value": "65000000000000000000000",
        "timestamp": 1785859200000,
        "window_s": 30,
    }
    payload[field] = value
    message = json.dumps(
        {"topic": "crypto_prices_twap_thirty", "type": "update", "payload": payload}
    )

    assert PolymarketChainlinkTwapStream.parse_message(message) is None


def test_twap_parser_requires_exact_value() -> None:
    message = json.dumps(
        {
            "topic": "crypto_prices_twap_thirty",
            "type": "update",
            "payload": {
                "symbol": "btc/usd",
                "value": 65000.5,
                "timestamp": 1785859200000,
                "window_s": 30,
            },
        }
    )

    with pytest.raises(ValueError, match="full_accuracy_value"):
        PolymarketChainlinkTwapStream.parse_message(message)
