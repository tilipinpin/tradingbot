from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import requests
from requests import RequestException

from src.fair_value import btc_up_probability, choose_theoretical_action, estimate_sigma_per_sqrt_second
from src.polymarket import ClobDataClient, ClobTradingClient, GammaClient, Market, OrderBookQuote
from src.price_signal import SpotPriceClient


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("btc-updown-watch")


SLUG_PATTERN = re.compile(r"^(btc-updown-5m-)(\d+)$")
GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass(frozen=True)
class AutoTradeSignal:
    side: str
    token_id: str
    price: Decimal
    reason: str


@dataclass
class PaperPosition:
    slug: str
    side: str
    entry_price: Decimal
    stake: Decimal
    shares: Decimal
    settled: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch rolling BTC 5m Up/Down Polymarket windows.")
    parser.add_argument("--slug", required=True, help="Current BTC 5m event slug or Polymarket event URL.")
    parser.add_argument("--duration", type=int, default=900, help="Total watch duration in seconds.")
    parser.add_argument("--interval", type=int, default=10, help="Polling interval in seconds.")
    parser.add_argument("--price-source", default="COINGECKO", help="BINANCE, COINBASE, or COINGECKO.")
    parser.add_argument("--edge", default="0.06", help="Minimum theoretical edge for BUY_UP/BUY_DOWN.")
    parser.add_argument("--fallback-sigma", default="0.00005", help="Fallback volatility per sqrt(second).")
    parser.add_argument("--clob-host", default="https://clob.polymarket.com")
    parser.add_argument("--auto-trade", action="store_true", help="Enable automatic signal detection.")
    parser.add_argument("--live-trading", action="store_true", help="Actually submit orders. Requires wallet env vars.")
    parser.add_argument("--strategy", default="near_even_momentum", choices=["near_even_momentum"])
    parser.add_argument("--decision-seconds-before-end", type=int, default=120)
    parser.add_argument("--min-entry", default="0.40")
    parser.add_argument("--max-entry", default="0.50")
    parser.add_argument("--order-size", default="5")
    parser.add_argument("--max-trades", type=int, default=1, help="Max live/dry-run trade signals per window.")
    parser.add_argument("--paper-trading", action="store_true", help="Track a simulated bankroll and settle windows.")
    parser.add_argument("--paper-bankroll", default="20", help="Starting simulated bankroll in USDC.")
    parser.add_argument("--paper-stake", default="1", help="Simulated USDC stake per signal.")
    parser.add_argument("--stop-when-bust", action="store_true", help="Exit when paper bankroll reaches zero.")
    parser.add_argument("--chain-id", type=int, default=137)
    parser.add_argument("--signature-type", type=int, default=0)
    parser.add_argument("--private-key-env", default="PRIVATE_KEY")
    parser.add_argument("--funder-address-env", default="FUNDER_ADDRESS")
    return parser.parse_args()


def slug_from_value(value: str) -> str:
    cleaned = value.strip().rstrip("/")
    if "/" not in cleaned:
        return cleaned
    return cleaned.split("/")[-1]


def next_5m_slug(slug: str) -> str:
    match = SLUG_PATTERN.match(slug)
    if not match:
        raise ValueError(f"Cannot derive next 5m slug from: {slug}")
    return f"{match.group(1)}{int(match.group(2)) + 300}"


def load_updown_market(gamma: GammaClient, slug: str) -> Market | None:
    try:
        event = gamma.event_by_slug(slug)
    except (LookupError, RequestException) as exc:
        logger.warning("Could not load event %s: %s", slug, exc)
        return None
    if not event.markets:
        logger.warning("Event %s has no markets", slug)
        return None
    return event.markets[0]


def fetch_winner(slug: str) -> str | None:
    response = requests.get(f"{GAMMA_API}/events", params={"slug": slug, "limit": 1}, timeout=20)
    response.raise_for_status()
    events = response.json()
    if not events:
        return None
    markets = events[0].get("markets") or []
    if not markets:
        return None
    outcome_prices = markets[0].get("outcomePrices") or ""
    try:
        prices = json.loads(outcome_prices)
    except json.JSONDecodeError:
        return None
    if len(prices) < 2:
        return None
    if Decimal(str(prices[0])) == Decimal("1"):
        return "UP"
    if Decimal(str(prices[1])) == Decimal("1"):
        return "DOWN"
    return None


def quote_outcomes(clob: ClobDataClient, market: Market) -> tuple[OrderBookQuote | None, OrderBookQuote | None]:
    up_quote = None
    down_quote = None
    try:
        up_quote = clob.quote(market.token_ids[0])
    except RequestException as exc:
        logger.warning("Could not fetch Up quote for %s: %s", market.slug, exc)
    try:
        down_quote = clob.quote(market.token_ids[1])
    except RequestException as exc:
        logger.warning("Could not fetch Down quote for %s: %s", market.slug, exc)
    return up_quote, down_quote


def choose_near_even_momentum_signal(
    market: Market,
    initial_up_ask: Decimal | None,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    decision_seconds_before_end: Decimal,
    min_entry: Decimal,
    max_entry: Decimal,
) -> AutoTradeSignal | None:
    if initial_up_ask is None or up_quote is None or down_quote is None:
        return None
    if seconds_to_end > decision_seconds_before_end or seconds_to_end <= 0:
        return None
    if up_quote.ask is None or down_quote.ask is None:
        return None

    if up_quote.ask >= initial_up_ask:
        side = "UP"
        token_id = market.token_ids[0]
        entry = up_quote.ask
        reason = f"UP ask {up_quote.ask} >= initial UP ask {initial_up_ask}"
    else:
        side = "DOWN"
        token_id = market.token_ids[1]
        entry = down_quote.ask
        reason = f"UP ask {up_quote.ask} < initial UP ask {initial_up_ask}"

    if entry < min_entry or entry > max_entry:
        return None

    return AutoTradeSignal(
        side=side,
        token_id=token_id,
        price=entry,
        reason=f"near_even_momentum entry={entry} seconds_left={int(seconds_to_end)}; {reason}",
    )


def build_live_trader(args: argparse.Namespace) -> ClobTradingClient | None:
    if not args.live_trading:
        return None
    private_key = os.getenv(args.private_key_env)
    funder_address = os.getenv(args.funder_address_env)
    if not private_key or not funder_address:
        raise ValueError(
            f"--live-trading requires {args.private_key_env} and {args.funder_address_env} environment variables"
        )
    return ClobTradingClient(
        host=args.clob_host,
        chain_id=args.chain_id,
        private_key=private_key,
        funder_address=funder_address,
        signature_type=args.signature_type,
    )


def open_paper_position(
    positions: list[PaperPosition],
    bankroll: Decimal,
    market_slug: str,
    signal: AutoTradeSignal,
    stake: Decimal,
) -> Decimal:
    if stake <= 0:
        raise ValueError("--paper-stake must be positive")
    if signal.price <= 0:
        raise ValueError("Cannot paper trade at non-positive price")
    if bankroll < stake:
        logger.info("PAPER_SKIP insufficient bankroll=%s stake=%s", bankroll, stake)
        return bankroll

    shares = stake / signal.price
    positions.append(
        PaperPosition(
            slug=market_slug,
            side=signal.side,
            entry_price=signal.price,
            stake=stake,
            shares=shares,
        )
    )
    bankroll -= stake
    logger.info(
        "PAPER_OPEN slug=%s side=%s entry=%s stake=%s shares=%s bankroll=%s",
        market_slug,
        signal.side,
        signal.price,
        stake,
        shares.quantize(Decimal("0.0001")),
        bankroll.quantize(Decimal("0.0001")),
    )
    return bankroll


def settle_paper_positions(positions: list[PaperPosition], slug: str, bankroll: Decimal) -> Decimal:
    unsettled = [position for position in positions if position.slug == slug and not position.settled]
    if not unsettled:
        return bankroll

    try:
        winner = fetch_winner(slug)
    except RequestException as exc:
        logger.warning("PAPER_SETTLE_WAIT slug=%s fetch winner failed: %s", slug, exc)
        return bankroll
    if winner is None:
        logger.info("PAPER_SETTLE_WAIT slug=%s winner not available yet", slug)
        return bankroll

    for position in unsettled:
        position.settled = True
        payout = position.shares if position.side == winner else Decimal("0")
        bankroll += payout
        profit = payout - position.stake
        logger.info(
            "PAPER_SETTLE slug=%s side=%s winner=%s payout=%s profit=%s bankroll=%s",
            slug,
            position.side,
            winner,
            payout.quantize(Decimal("0.0001")),
            profit.quantize(Decimal("0.0001")),
            bankroll.quantize(Decimal("0.0001")),
        )
    return bankroll


def settle_all_paper_positions(positions: list[PaperPosition], bankroll: Decimal) -> Decimal:
    slugs = sorted({position.slug for position in positions if not position.settled})
    for slug in slugs:
        bankroll = settle_paper_positions(positions, slug, bankroll)
    return bankroll


def _seconds_to_start(market: Market, now: datetime) -> Decimal:
    if market.event_start_time is None:
        return Decimal("0")
    return Decimal(str((market.event_start_time - now).total_seconds()))


def _seconds_to_end(market: Market, now: datetime) -> Decimal:
    if market.end_time is None:
        return Decimal("300")
    return Decimal(str((market.end_time - now).total_seconds()))


def watch() -> None:
    args = parse_args()
    gamma = GammaClient()
    clob = ClobDataClient(args.clob_host)
    trader = build_live_trader(args)
    price_client = SpotPriceClient(args.price_source)
    slug = slug_from_value(args.slug)
    stop_at = time.monotonic() + args.duration
    current_market: Market | None = None
    start_price: Decimal | None = None
    initial_up_ask: Decimal | None = None
    prices: list[Decimal] = []
    signals_this_window = 0
    edge_threshold = Decimal(args.edge)
    fallback_sigma = Decimal(args.fallback_sigma)
    min_entry = Decimal(args.min_entry)
    max_entry = Decimal(args.max_entry)
    order_size = Decimal(args.order_size)
    decision_seconds_before_end = Decimal(str(args.decision_seconds_before_end))
    paper_bankroll = Decimal(args.paper_bankroll)
    paper_stake = Decimal(args.paper_stake)
    paper_positions: list[PaperPosition] = []

    if args.live_trading and not args.auto_trade:
        raise ValueError("--live-trading requires --auto-trade")
    if args.live_trading:
        logger.warning("LIVE TRADING ENABLED. Orders may be submitted.")
    elif args.paper_trading:
        logger.info(
            "PAPER TRADING mode. Starting bankroll=%s stake_per_signal=%s",
            paper_bankroll,
            paper_stake,
        )
    else:
        logger.info("DRY RUN mode. No orders will be submitted.")

    while time.monotonic() < stop_at:
        now = datetime.now(timezone.utc)
        if args.paper_trading:
            paper_bankroll = settle_all_paper_positions(paper_positions, paper_bankroll)
            if args.stop_when_bust and paper_bankroll <= 0 and all(position.settled for position in paper_positions):
                logger.info("PAPER_BUST bankroll=%s. Exiting.", paper_bankroll)
                return

        if current_market is None or _seconds_to_end(current_market, now) <= 0:
            if current_market is not None:
                slug = next_5m_slug(current_market.slug)
                logger.info("Window ended. Looking for next slug: %s", slug)
            current_market = load_updown_market(gamma, slug)
            start_price = None
            initial_up_ask = None
            prices = []
            signals_this_window = 0
            if current_market is None:
                time.sleep(args.interval)
                continue
            if _seconds_to_end(current_market, datetime.now(timezone.utc)) <= 0:
                logger.info("Skipping expired window: %s", current_market.slug)
                slug = next_5m_slug(current_market.slug)
                current_market = None
                continue
            logger.info(
                "Watching %s | start=%s end=%s liquidity=%s outcomes=%s",
                current_market.slug,
                current_market.event_start_time,
                current_market.end_time,
                current_market.liquidity,
                current_market.outcomes,
            )

        seconds_to_start = _seconds_to_start(current_market, now)
        seconds_to_end = _seconds_to_end(current_market, now)
        spot = price_client.btc_usd()

        if seconds_to_start > 0:
            logger.info(
                "Waiting for %s to start in %ss | spot=%s",
                current_market.slug,
                int(seconds_to_start),
                spot.price,
            )
            time.sleep(min(args.interval, max(1, float(seconds_to_start))))
            continue

        if start_price is None:
            start_price = spot.price
            prices = [spot.price]
            logger.info("Captured start_price=%s for %s", start_price, current_market.slug)
        else:
            prices.append(spot.price)

        sigma = estimate_sigma_per_sqrt_second(prices, Decimal(args.interval), fallback_sigma)
        fair = btc_up_probability(start_price, spot.price, max(Decimal("0"), seconds_to_end), sigma)
        up_quote, down_quote = quote_outcomes(clob, current_market)
        up_ask = up_quote.ask if up_quote else None
        down_ask = down_quote.ask if down_quote else None
        if initial_up_ask is None and up_ask is not None:
            initial_up_ask = up_ask
            logger.info("Captured initial_up_ask=%s for %s", initial_up_ask, current_market.slug)
        action = choose_theoretical_action(fair.probability_up, up_ask, down_ask, edge_threshold)

        logger.info(
            "%s seconds_left=%s spot=%s start=%s p_up=%.4f up=%s down=%s fair_action=%s",
            current_market.slug,
            int(seconds_to_end),
            spot.price,
            start_price,
            float(fair.probability_up),
            up_quote,
            down_quote,
            action,
        )

        if args.auto_trade and signals_this_window < args.max_trades:
            signal = choose_near_even_momentum_signal(
                current_market,
                initial_up_ask,
                up_quote,
                down_quote,
                seconds_to_end,
                decision_seconds_before_end,
                min_entry,
                max_entry,
            )
            if signal is not None:
                signals_this_window += 1
                logger.info(
                    "AUTO_SIGNAL %s side=%s price=%s size=%s reason=%s",
                    current_market.slug,
                    signal.side,
                    signal.price,
                    order_size,
                    signal.reason,
                )
                if trader is None:
                    if args.paper_trading:
                        paper_bankroll = open_paper_position(
                            paper_positions,
                            paper_bankroll,
                            current_market.slug,
                            signal,
                            paper_stake,
                        )
                        if args.stop_when_bust and paper_bankroll <= 0:
                            logger.info("PAPER_BUST bankroll=%s. Exiting after open position.", paper_bankroll)
                            return
                    else:
                        logger.info("DRY RUN: would buy %s at %s x %s", signal.side, signal.price, order_size)
                else:
                    response = trader.buy_limit(
                        token_id=signal.token_id,
                        price=signal.price,
                        size=order_size,
                        tick_size=current_market.minimum_tick_size,
                        neg_risk=current_market.neg_risk,
                    )
                    logger.info("LIVE ORDER response=%s", response)

        time.sleep(args.interval)

    if args.paper_trading:
        paper_bankroll = settle_all_paper_positions(paper_positions, paper_bankroll)
        open_positions = sum(1 for position in paper_positions if not position.settled)
        logger.info(
            "PAPER_SUMMARY bankroll=%s positions=%s open_positions=%s",
            paper_bankroll.quantize(Decimal("0.0001")),
            len(paper_positions),
            open_positions,
        )


if __name__ == "__main__":
    watch()
