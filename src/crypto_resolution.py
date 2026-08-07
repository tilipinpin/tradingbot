from __future__ import annotations

from enum import Enum


class CryptoResolutionMode(str, Enum):
    AUTO = "auto"
    LEGACY = "legacy"
    TWAP_30 = "twap30"


_TWAP_MARKERS = (
    "30-second twap",
    "30 second twap",
    "30-second time-weighted average",
    "30 second time-weighted average",
    "crypto_prices_twap_thirty",
)


def detect_crypto_resolution_mode(
    rules_text: str,
    configured: str = CryptoResolutionMode.AUTO.value,
) -> CryptoResolutionMode:
    """Select the price convention from the market's own rules."""
    try:
        requested = CryptoResolutionMode(configured.lower())
    except ValueError as exc:
        raise ValueError("crypto resolution mode must be auto, legacy, or twap30") from exc
    if requested is not CryptoResolutionMode.AUTO:
        return requested
    normalized = " ".join((rules_text or "").lower().split())
    mentions_30_second_average = "30" in normalized and (
        "twap" in normalized
        or "time-weighted average" in normalized
        or "time weighted average" in normalized
    )
    if mentions_30_second_average or any(marker in normalized for marker in _TWAP_MARKERS):
        return CryptoResolutionMode.TWAP_30
    return CryptoResolutionMode.LEGACY
