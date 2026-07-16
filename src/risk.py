from __future__ import annotations

from decimal import Decimal

from eth_account import Account

from src.config import Settings
from src.strategy import TradeIntent


def validate_settings_for_live_trading(settings: Settings) -> None:
    if not settings.live_trading:
        return
    if settings.dry_run:
        raise ValueError("LIVE_TRADING=true requires DRY_RUN=false")
    if not settings.private_key:
        raise ValueError("PRIVATE_KEY is required for live trading")
    if not settings.funder_address:
        raise ValueError("FUNDER_ADDRESS is required for live trading")
    if settings.signature_type == 3:
        owner = Account.from_key(settings.private_key).address
        if owner.lower() == settings.funder_address.lower():
            raise ValueError(
                "SIGNATURE_TYPE=3 requires FUNDER_ADDRESS to be the deployed "
                "deposit wallet, not the owner EOA"
            )


def validate_intent(intent: TradeIntent, settings: Settings) -> None:
    notional = intent.price * intent.size
    if intent.price <= Decimal("0") or intent.price >= Decimal("1"):
        raise ValueError(f"Refusing invalid price: {intent.price}")
    if intent.size <= Decimal("0"):
        raise ValueError(f"Refusing invalid size: {intent.size}")
    if notional > settings.max_daily_usd:
        raise ValueError(f"Refusing order notional {notional}; exceeds MAX_DAILY_USD")
