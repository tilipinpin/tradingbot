from __future__ import annotations

import logging
from requests import RequestException

from src.config import Settings, load_settings
from src.polymarket import (
    ClobDataClient,
    ClobTradingClient,
    GammaClient,
    choose_btc_markets,
    filter_markets_by_liquidity,
    rank_markets_by_liquidity,
)
from src.price_signal import SpotPrice, SpotPriceClient, build_threshold_signal
from src.risk import validate_intent, validate_settings_for_live_trading
from src.strategy import build_buy_intent


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("polymarket-btc-bot")


def run(settings: Settings) -> None:
    validate_settings_for_live_trading(settings)

    gamma = GammaClient()
    if settings.market_slug:
        markets = [gamma.market_by_slug(settings.market_slug)]
        logger.info("Loaded market slug %s", settings.market_slug)
    elif settings.event_slug:
        event = gamma.event_by_slug(settings.event_slug)
        markets = choose_btc_markets(
            list(event.markets),
            settings.market_query,
            settings.market_direction_query,
        )
        logger.info("Loaded event %s with %s matching markets", event.slug, len(markets))
    else:
        markets = choose_btc_markets(
            gamma.active_markets(settings.market_limit),
            settings.market_query,
            settings.market_direction_query,
        )

    markets = filter_markets_by_liquidity(markets, settings.min_market_liquidity)
    markets = rank_markets_by_liquidity(markets)[: settings.max_markets_per_run]

    if not markets:
        logger.warning(
            "No BTC/Bitcoin direction markets found. Set POLYMARKET_EVENT_SLUG or POLYMARKET_MARKET_SLUG directly."
        )
        return
    logger.info(
        "Selected %s market(s): %s",
        len(markets),
        "; ".join(f"{market.slug} liquidity={market.liquidity}" for market in markets),
    )

    spot_price = None
    if settings.trade_outcome == "AUTO":
        if settings.manual_btc_price is not None:
            spot_price = SpotPrice("BTC/USD", settings.manual_btc_price, "MANUAL")
        else:
            try:
                spot_price = SpotPriceClient(settings.spot_price_source).btc_usd()
            except Exception as exc:
                logger.warning(
                    "Could not fetch BTC spot price from %s: %s. Set MANUAL_BTC_PRICE or TRADE_OUTCOME=YES/NO.",
                    settings.spot_price_source,
                    exc,
                )
                return
        logger.info("Spot price: %s %s from %s", spot_price.symbol, spot_price.price, spot_price.source)

    data_client = ClobDataClient(settings.clob_host)
    trader = None
    if settings.live_trading:
        assert settings.private_key and settings.funder_address
        trader = ClobTradingClient(
            host=settings.clob_host,
            chain_id=settings.chain_id,
            private_key=settings.private_key,
            funder_address=settings.funder_address,
            signature_type=settings.signature_type,
        )

    for market in markets:
        outcome = settings.trade_outcome
        signal_reason = None
        if outcome == "AUTO":
            assert spot_price is not None
            signal = build_threshold_signal(market.question, spot_price, settings.threshold_buffer_bps)
            if signal is None:
                logger.info("Skip %s; no clear threshold signal", market.question)
                continue
            outcome = signal.outcome
            signal_reason = signal.reason

        token_id = market.token_ids[0] if outcome == "YES" else market.token_ids[1]
        try:
            quote = trader.quote(token_id) if trader else data_client.quote(token_id)
        except RequestException as exc:
            logger.warning("Skip %s; failed to fetch CLOB quote: %s", market.question, exc)
            continue
        intent = build_buy_intent(
            market=market,
            outcome=outcome,
            quote=quote,
            max_price=settings.max_price,
            size=settings.order_size,
            signal_reason=signal_reason,
        )
        if intent is None:
            logger.info("Skip %s; quote=%s", market.question, quote)
            continue

        validate_intent(intent, settings)
        logger.info(
            "Intent: BUY %s %s at %s x %s; liquidity=%s; reason=%s; slug=%s",
            intent.outcome,
            intent.market.question,
            intent.price,
            intent.size,
            intent.market.liquidity,
            intent.reason,
            intent.market.slug,
        )

        if not settings.live_trading:
            logger.info("DRY RUN: not posting order")
            continue

        assert trader is not None
        response = trader.buy_limit(
            token_id=intent.token_id,
            price=intent.price,
            size=intent.size,
            tick_size=intent.market.minimum_tick_size,
            neg_risk=intent.market.neg_risk,
        )
        logger.info("Order response: %s", response)


def main() -> None:
    run(load_settings())


if __name__ == "__main__":
    main()
