from __future__ import annotations

from decimal import Decimal
from enum import Enum


class CryptoResolutionMode(str, Enum):
    AUTO = "auto"
    LEGACY = "legacy"
    TWAP_60 = "twap60"


_TWAP_MARKERS = (
    "60-second twap",
    "60 second twap",
    "60-second time-weighted average",
    "60 second time-weighted average",
    "crypto_prices_twap_sixty",
    "twap-60s",
    "btc-5m-twap-60",
)

OFFICIAL_BTC_USD_TWAP_60_SOURCE = "btc-usd-twap-60s-streams"


def has_official_btc_5m_twap_rule(rules_text: str) -> bool:
    """Confirm the market names the exact Chainlink feed used for settlement."""
    normalized = " ".join((rules_text or "").lower().split())
    return (
        OFFICIAL_BTC_USD_TWAP_60_SOURCE in normalized
        and "chainlink" in normalized
        and "twap" in normalized
    )


def resolve_btc_5m_twap(open_price: Decimal, end_twap: Decimal) -> str:
    """Apply the official BTC 5m rule; equality resolves to UP."""
    if open_price <= 0 or end_twap <= 0:
        raise ValueError(
            f"official settlement prices must be positive: "
            f"open={open_price} end_twap={end_twap}"
        )
    return "UP" if end_twap >= open_price else "DOWN"


def detect_crypto_resolution_mode(
    rules_text: str,
    configured: str = CryptoResolutionMode.AUTO.value,
) -> CryptoResolutionMode:
    """Select the price convention from the market's own rules."""
    try:
        normalized_config = configured.lower()
        if normalized_config == "twap30":
            normalized_config = CryptoResolutionMode.TWAP_60.value
        requested = CryptoResolutionMode(normalized_config)
    except ValueError as exc:
        raise ValueError("crypto resolution mode must be auto, legacy, or twap60") from exc
    if requested is not CryptoResolutionMode.AUTO:
        return requested
    normalized = " ".join((rules_text or "").lower().split())
    if "twap" in normalized or any(marker in normalized for marker in _TWAP_MARKERS):
        return CryptoResolutionMode.TWAP_60
    return CryptoResolutionMode.LEGACY
