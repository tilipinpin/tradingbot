from __future__ import annotations

import argparse
import atexit
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from requests import RequestException

from src import __version__
from src.fair_value import btc_up_probability, choose_theoretical_action, estimate_sigma_per_sqrt_second
from src.market_recorder import JsonlSnapshotWriter, build_snapshot
from src.polymarket import (
    ClobDataClient,
    ClobTradingClient,
    GammaClient,
    Market,
    OrderBookQuote,
)
from src.price_alignment import PolymarketPriceToBeatClient
from src.price_signal import SpotPriceClient
from src.telegram_commands import LIVE_STRATEGIES, STRATEGY_LABELS
from src.telegram_notify import TradingNotificationService


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("btc-updown-watch")


SLUG_PATTERN = re.compile(r"^(btc-updown-5m-)(\d+)$")
GAMMA_API = "https://gamma-api.polymarket.com"
_ACTIVE_NOTIFICATIONS: TradingNotificationService | None = None


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
    fee: Decimal = Decimal("0")
    settled: bool = False
    profit: Decimal | None = None
    accounted: bool = False


@dataclass(frozen=True)
class HedgeRiskEvaluation:
    reduces_max_loss: bool
    max_loss_before: Decimal
    max_loss_after: Decimal
    pnl_up_after: Decimal
    pnl_down_after: Decimal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch rolling BTC 5m Up/Down Polymarket windows.")
    parser.add_argument("--slug", required=True, help="Current BTC 5m event slug or Polymarket event URL.")
    parser.add_argument("--duration", type=int, default=0, help="Total watch duration in seconds; 0 means unlimited.")
    parser.add_argument("--interval", type=int, default=10, help="Polling interval in seconds.")
    parser.add_argument("--price-source", default="POLYMARKET_CHAINLINK", help="POLYMARKET_CHAINLINK (strict default), AUTO (Chainlink + free exchanges), CHAINLINK, BINANCE, COINBASE, KRAKEN, or COINGECKO.")
    parser.add_argument("--edge", default="0.06", help="Minimum theoretical edge for BUY_UP/BUY_DOWN.")
    parser.add_argument(
        "--fallback-sigma",
        default="0.00005",
        help="Fallback and long-run floor for volatility per sqrt(second).",
    )
    parser.add_argument("--clob-host", default="https://clob.polymarket.com")
    parser.add_argument("--market-data-timeout", type=int, default=3, help="Per-request timeout for CLOB and spot data.")
    parser.add_argument("--ws-proxy", help="Optional WebSocket proxy, e.g. socks5h://127.0.0.1:7898.")
    parser.add_argument("--record-jsonl", help="Append every complete market snapshot to this JSONL file.")
    parser.add_argument("--auto-trade", action="store_true", help="Enable automatic signal detection.")
    parser.add_argument("--live-trading", action="store_true", help="Actually submit orders. Requires wallet env vars.")
    parser.add_argument(
        "--strategy",
        default="fair_value_edge",
        choices=list(STRATEGY_LABELS),
    )
    parser.add_argument("--decision-seconds-before-end", type=int, default=90)
    parser.add_argument("--min-seconds-before-end", type=int, default=25)
    parser.add_argument("--signal-confirmations", type=int, default=2)
    parser.add_argument("--trend-confirmation-samples", type=int, default=3)
    parser.add_argument("--confirmation-jump-sigma-multiplier", default="1.25")
    parser.add_argument("--confirmation-min-jump-usd", default="3.00")
    parser.add_argument("--hedge-signal-confirmations", type=int, default=2)
    parser.add_argument("--hedge-min-win-probability", default="0.62")
    parser.add_argument("--hedge-fee-rate", default="0.07")
    parser.add_argument("--hedge-entry-start-seconds", type=int, default=20)
    parser.add_argument("--hedge-entry-cutoff-seconds", type=int, default=1)
    parser.add_argument("--hedge-market-reversal-threshold", default="0.65")
    parser.add_argument("--hedge-market-reversal-confirmations", type=int, default=2)
    parser.add_argument("--hedge-max-entry", default="0.99")
    parser.add_argument("--hedge-max-live-notional", default="5.00")
    parser.add_argument("--final-poll-seconds", type=int, default=30)
    parser.add_argument("--final-poll-interval", type=float, default=1.0)
    parser.add_argument("--max-spot-age", type=int, default=20, help="Maximum cached spot-price age allowed for entries.")
    parser.add_argument(
        "--price-to-beat-proxy",
        help="Optional HTTP/SOCKS proxy for the Polymarket crypto-price endpoint; defaults to --ws-proxy.",
    )
    parser.add_argument(
        "--price-alignment-jsonl",
        help="Append one Price to Beat verification record per window.",
    )
    parser.add_argument(
        "--max-price-alignment-difference",
        default="0.50",
        help="Warn when official openPrice differs from the boundary Chainlink sample by more than this many USD.",
    )
    parser.add_argument(
        "--max-boundary-sample-offset-ms",
        type=int,
        default=1000,
        help="Maximum timestamp distance between a cached Chainlink sample and the exact market boundary.",
    )
    parser.add_argument("--min-win-probability", default="0.62")
    parser.add_argument(
        "--probability-shrinkage",
        default="1.00",
        help="Shrink fair-value probability toward 0.50 before evaluating edge; 1 disables calibration.",
    )
    parser.add_argument("--low-entry-cutoff", default="0.55")
    parser.add_argument("--low-entry-min-win-probability", default="0.68")
    parser.add_argument("--low-entry-confirmation-samples", type=int, default=3)
    parser.add_argument("--min-entry", default="0.50")
    parser.add_argument("--max-entry", default="0.78")
    parser.add_argument("--max-spread", default="0.04", help="Max bid/ask spread allowed for the selected side.")
    parser.add_argument("--min-ask-sum", default="0.90", help="Skip markets where Up ask + Down ask is below this.")
    parser.add_argument("--max-ask-sum", default="1.10", help="Skip markets where Up ask + Down ask is above this.")
    parser.add_argument("--order-size", default="5")
    parser.add_argument("--max-trades", type=int, default=2, help="Max matched live trades per window.")
    parser.add_argument(
        "--max-live-orders",
        type=int,
        default=0,
        help="Hard session cap on live order attempts; 0 means unlimited.",
    )
    parser.add_argument("--max-live-notional", default="3.75", help="Hard principal cap per live order in pUSD.")
    parser.add_argument(
        "--late-max-live-notional",
        default="4.70",
        help="Hard principal cap per late_favorite live order in pUSD.",
    )
    parser.add_argument("--live-order-type", choices=["FOK"], default="FOK")
    parser.add_argument(
        "--live-summary-json",
        default="data/live_trade_summary.json",
        help="Write the latest live session result as JSON.",
    )
    parser.add_argument("--max-consecutive-losses", type=int, default=2)
    parser.add_argument("--pause-windows-after-losses", type=int, default=2)
    parser.add_argument("--paper-trading", action="store_true", help="Track a simulated bankroll and settle windows.")
    parser.add_argument("--paper-bankroll", default="20", help="Starting simulated bankroll in USDC.")
    parser.add_argument("--paper-stake", default="1", help="Simulated USDC stake per signal.")
    parser.add_argument("--late-entry-start-seconds", type=int, default=55)
    parser.add_argument("--late-entry-cutoff-seconds", type=int, default=8)
    parser.add_argument("--late-min-entry", default="0.65")
    parser.add_argument("--late-max-entry", default="0.94")
    parser.add_argument("--late-min-win-probability", default="0.80")
    parser.add_argument("--late-edge-margin", default="0.00")
    parser.add_argument("--late-min-expected-roi", default="0.02")
    parser.add_argument("--late-fee-rate", default="0.07")
    parser.add_argument("--late-max-spread", default="0.03")
    parser.add_argument("--late-min-ask-sum", default="0.96")
    parser.add_argument("--late-max-ask-sum", default="1.04")
    parser.add_argument("--late-confirmation-samples", type=int, default=2)
    parser.add_argument("--late-no-cross-samples", type=int, default=3)
    parser.add_argument("--late-signal-confirmations", type=int, default=1)
    parser.add_argument("--late-min-lead-bps", default="1.0")
    parser.add_argument("--late-max-pullback-bps", default="1.50")
    parser.add_argument("--late-max-pullback-ratio", default="0.50")
    parser.add_argument("--late-volatility-buffer-multiplier", default="0.50")
    parser.add_argument("--late-pause-windows-after-loss", type=int, default=0)
    parser.add_argument("--stop-when-bust", action="store_true", help="Exit when paper bankroll reaches zero.")
    parser.add_argument("--chain-id", type=int, default=int(os.getenv("CHAIN_ID", "137")))
    parser.add_argument("--signature-type", type=int, default=int(os.getenv("SIGNATURE_TYPE", "0")))
    parser.add_argument("--private-key-env", default="PRIVATE_KEY")
    parser.add_argument("--funder-address-env", default="FUNDER_ADDRESS")
    parser.add_argument("--env-file", help="Optional dotenv file containing wallet settings.")
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


def official_open_retry_expired(
    seconds_to_end: Decimal,
    strategy: str,
    fair_entry_start_seconds: Decimal,
    late_entry_start_seconds: Decimal,
) -> bool:
    entry_start = (
        late_entry_start_seconds
        if strategy == "late_favorite"
        else fair_entry_start_seconds
    )
    return seconds_to_end <= entry_start


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
    try:
        up_quote, down_quote = clob.quotes(market.token_ids)
    except RequestException as exc:
        logger.warning("Could not fetch outcome quotes for %s: %s", market.slug, exc)
        return None, None
    return up_quote, down_quote


def quote_spread(quote: OrderBookQuote | None) -> Decimal | None:
    if quote is None or quote.bid is None or quote.ask is None:
        return None
    return quote.ask - quote.bid


def quotes_pass_sanity_checks(
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    max_spread: Decimal,
    min_ask_sum: Decimal,
    max_ask_sum: Decimal,
) -> tuple[bool, str]:
    if up_quote is None or down_quote is None:
        return False, "missing quote"
    if up_quote.bid is None or up_quote.ask is None or down_quote.bid is None or down_quote.ask is None:
        return False, "missing bid/ask"
    quotes = (("UP", up_quote), ("DOWN", down_quote))
    for side, quote in quotes:
        if quote.bid < 0 or quote.ask <= 0 or quote.bid > 1 or quote.ask > 1:
            return False, f"{side} quote outside 0-1"
        if quote.bid > quote.ask:
            return False, f"{side} bid {quote.bid} > ask {quote.ask}"
        if quote.ask - quote.bid > max_spread:
            return False, f"{side} spread {quote.ask - quote.bid} > {max_spread}"
    ask_sum = up_quote.ask + down_quote.ask
    if ask_sum < min_ask_sum or ask_sum > max_ask_sum:
        return False, f"ask_sum {ask_sum} outside {min_ask_sum}-{max_ask_sum}"
    return True, "ok"


def recent_spot_samples_support_side(
    prices: list[Decimal],
    start_price: Decimal,
    side: str,
    sample_count: int,
) -> bool:
    if sample_count < 1 or len(prices) < sample_count:
        return False
    recent = prices[-sample_count:]
    if side == "UP":
        return all(price > start_price for price in recent) and recent[-1] >= recent[0]
    if side == "DOWN":
        return all(price < start_price for price in recent) and recent[-1] <= recent[0]
    return False


def adverse_jump_exceeds_dynamic_threshold(
    prices: list[Decimal],
    side: str,
    sigma_per_sqrt_second: Decimal,
    interval_seconds: Decimal,
    sigma_multiplier: Decimal,
    minimum_jump_usd: Decimal,
) -> tuple[bool, Decimal, Decimal]:
    if (
        len(prices) < 2
        or prices[-2] <= 0
        or interval_seconds <= 0
        or sigma_per_sqrt_second < 0
        or sigma_multiplier < 0
        or minimum_jump_usd < 0
    ):
        return False, Decimal("0"), minimum_jump_usd
    dynamic_threshold = (
        prices[-2]
        * sigma_per_sqrt_second
        * Decimal(str(math.sqrt(float(interval_seconds))))
        * sigma_multiplier
    )
    threshold = max(minimum_jump_usd, dynamic_threshold)
    move = prices[-1] - prices[-2]
    adverse_move = -move if side == "UP" else move if side == "DOWN" else Decimal("0")
    return adverse_move > threshold, max(Decimal("0"), adverse_move), threshold


def shrink_probability_toward_even(probability: Decimal, shrinkage: Decimal) -> Decimal:
    if probability < 0 or probability > 1:
        raise ValueError("probability must be between zero and one")
    if shrinkage < 0 or shrinkage > 1:
        raise ValueError("probability shrinkage must be between zero and one")
    return Decimal("0.5") + shrinkage * (probability - Decimal("0.5"))


def late_spot_buffer_metrics(
    prices: list[Decimal],
    start_price: Decimal,
    side: str,
    sample_count: int,
) -> tuple[Decimal, Decimal] | None:
    if start_price <= 0 or sample_count < 1 or len(prices) < sample_count:
        return None
    recent = prices[-sample_count:]
    current = recent[-1]
    if side == "UP":
        if not all(price > start_price for price in recent) or current < recent[0]:
            return None
        lead_bps = (current / start_price - Decimal("1")) * Decimal("10000")
        pullback_bps = (max(recent) - current) / start_price * Decimal("10000")
    elif side == "DOWN":
        if not all(price < start_price for price in recent) or current > recent[0]:
            return None
        lead_bps = (Decimal("1") - current / start_price) * Decimal("10000")
        pullback_bps = (current - min(recent)) / start_price * Decimal("10000")
    else:
        return None
    return lead_bps, max(Decimal("0"), pullback_bps)


def late_spot_safety_metrics(
    prices: list[Decimal],
    start_price: Decimal,
    side: str,
    confirmation_samples: int,
    no_cross_samples: int,
) -> tuple[Decimal, Decimal, Decimal] | None:
    if no_cross_samples < confirmation_samples or len(prices) < no_cross_samples:
        return None
    no_cross = prices[-no_cross_samples:]
    if side == "UP" and not all(price > start_price for price in no_cross):
        return None
    if side == "DOWN" and not all(price < start_price for price in no_cross):
        return None

    buffer_metrics = late_spot_buffer_metrics(
        prices,
        start_price,
        side,
        confirmation_samples,
    )
    if buffer_metrics is None:
        return None
    lead_bps, pullback_bps = buffer_metrics
    if side == "UP":
        peak_lead_bps = (max(no_cross) / start_price - Decimal("1")) * Decimal("10000")
    else:
        peak_lead_bps = (Decimal("1") - min(no_cross) / start_price) * Decimal("10000")
    if peak_lead_bps <= 0:
        return None
    pullback_ratio = max(Decimal("0"), (peak_lead_bps - lead_bps) / peak_lead_bps)
    return lead_bps, pullback_bps, pullback_ratio


def strategy_trade_limit(strategy: str, configured_limit: int) -> int:
    if strategy == "late_favorite":
        return 1
    return configured_limit


def price_alignment_status(
    official_open_price: Decimal,
    boundary_price: Decimal | None,
    max_difference: Decimal,
) -> tuple[str, Decimal | None]:
    if boundary_price is None:
        return "UNVERIFIED_BOUNDARY_SAMPLE", None
    difference = boundary_price - official_open_price
    if abs(difference) > max_difference:
        return "MISMATCH_WARNING", difference
    return "VERIFIED", difference


def window_trade_count_after_attempt(current_count: int, *, live: bool, matched: bool = False) -> int:
    if live and not matched:
        return current_count
    return current_count + 1


def consume_pause_window(remaining_windows: int) -> tuple[bool, int]:
    if remaining_windows <= 0:
        return False, 0
    return True, remaining_windows - 1


def choose_fair_value_edge_signal(
    market: Market,
    probability_up: Decimal,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    decision_seconds_before_end: Decimal,
    min_entry: Decimal,
    max_entry: Decimal,
    edge_threshold: Decimal,
    max_spread: Decimal,
    min_ask_sum: Decimal,
    max_ask_sum: Decimal,
    min_seconds_before_end: Decimal = Decimal("0"),
    min_win_probability: Decimal = Decimal("0"),
    recent_spot_prices: list[Decimal] | None = None,
    start_price: Decimal | None = None,
    low_entry_cutoff: Decimal = Decimal("0.50"),
    low_entry_min_win_probability: Decimal = Decimal("0.68"),
    low_entry_confirmation_samples: int = 3,
    probability_shrinkage: Decimal = Decimal("1"),
) -> AutoTradeSignal | None:
    if seconds_to_end > decision_seconds_before_end or seconds_to_end < min_seconds_before_end:
        return None
    ok, reason = quotes_pass_sanity_checks(up_quote, down_quote, max_spread, min_ask_sum, max_ask_sum)
    if not ok:
        return None
    assert up_quote is not None and up_quote.ask is not None
    assert down_quote is not None and down_quote.ask is not None

    raw_probability_up = probability_up
    probability_up = shrink_probability_toward_even(probability_up, probability_shrinkage)
    down_probability = Decimal("1") - probability_up
    up_edge = probability_up - up_quote.ask
    down_edge = down_probability - down_quote.ask
    if up_edge >= down_edge:
        side = "UP"
        token_id = market.token_ids[0]
        entry = up_quote.ask
        edge = up_edge
    else:
        side = "DOWN"
        token_id = market.token_ids[1]
        entry = down_quote.ask
        edge = down_edge

    if entry < min_entry or entry > max_entry:
        return None
    selected_probability = probability_up if side == "UP" else down_probability
    required_probability = min_win_probability
    if entry < low_entry_cutoff:
        required_probability = max(required_probability, low_entry_min_win_probability)
        if (
            recent_spot_prices is None
            or start_price is None
            or not recent_spot_samples_support_side(
                recent_spot_prices,
                start_price,
                side,
                low_entry_confirmation_samples,
            )
        ):
            return None
    if selected_probability < required_probability:
        return None

    # Late and expensive entries need extra model margin because small price errors
    # have an outsized effect close to settlement.
    required_edge = edge_threshold
    if seconds_to_end < Decimal("45"):
        required_edge += Decimal("0.02")
    if entry > Decimal("0.65"):
        required_edge += (entry - Decimal("0.65")) * Decimal("0.25")
    if edge < required_edge:
        return None

    return AutoTradeSignal(
        side=side,
        token_id=token_id,
        price=entry,
        reason=(
            f"fair_value_edge entry={entry} edge={edge.quantize(Decimal('0.0001'))} "
            f"required_edge={required_edge.quantize(Decimal('0.0001'))} "
            f"required_probability={required_probability.quantize(Decimal('0.0001'))} "
            f"p_up={probability_up.quantize(Decimal('0.0001'))} "
            f"raw_p_up={raw_probability_up.quantize(Decimal('0.0001'))} "
            f"shrinkage={probability_shrinkage.quantize(Decimal('0.01'))} "
            f"seconds_left={int(seconds_to_end)}"
        ),
    )


def choose_protective_hedge_signal(
    market: Market,
    primary_side: str,
    probability_up: Decimal,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    decision_seconds_before_end: Decimal,
    min_seconds_before_end: Decimal,
    max_entry: Decimal,
    edge_threshold: Decimal,
    min_win_probability: Decimal,
    max_spread: Decimal,
    min_ask_sum: Decimal,
    max_ask_sum: Decimal,
) -> AutoTradeSignal | None:
    if seconds_to_end > decision_seconds_before_end or seconds_to_end < min_seconds_before_end:
        return None
    ok, _ = quotes_pass_sanity_checks(
        up_quote,
        down_quote,
        max_spread,
        min_ask_sum,
        max_ask_sum,
    )
    if not ok:
        return None
    assert up_quote is not None and down_quote is not None
    side = "DOWN" if primary_side == "UP" else "UP" if primary_side == "DOWN" else ""
    if not side:
        return None
    probability = probability_up if side == "UP" else Decimal("1") - probability_up
    quote = up_quote if side == "UP" else down_quote
    entry = quote.ask
    edge = probability - entry
    if probability < min_win_probability or entry <= 0 or entry > max_entry or edge < edge_threshold:
        return None
    token_id = market.token_ids[0] if side == "UP" else market.token_ids[1]
    return AutoTradeSignal(
        side=side,
        token_id=token_id,
        price=entry,
        reason=(
            f"protective_hedge primary_side={primary_side} entry={entry} "
            f"edge={edge.quantize(Decimal('0.0001'))} "
            f"probability={probability.quantize(Decimal('0.0001'))} "
            f"required_probability={min_win_probability.quantize(Decimal('0.0001'))} "
            f"seconds_left={int(seconds_to_end)}"
        ),
    )


def choose_market_reversal_hedge_signal(
    market: Market,
    primary_side: str,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    entry_start_seconds: Decimal,
    entry_cutoff_seconds: Decimal,
    reversal_bid_threshold: Decimal,
    max_entry: Decimal,
    max_spread: Decimal,
    min_ask_sum: Decimal,
    max_ask_sum: Decimal,
) -> AutoTradeSignal | None:
    if seconds_to_end > entry_start_seconds or seconds_to_end < entry_cutoff_seconds:
        return None
    ok, _ = quotes_pass_sanity_checks(
        up_quote,
        down_quote,
        max_spread,
        min_ask_sum,
        max_ask_sum,
    )
    if not ok:
        return None
    assert up_quote is not None and down_quote is not None
    side = "DOWN" if primary_side == "UP" else "UP" if primary_side == "DOWN" else ""
    if not side:
        return None
    quote = up_quote if side == "UP" else down_quote
    if quote.bid < reversal_bid_threshold or quote.ask <= 0 or quote.ask > max_entry:
        return None
    token_id = market.token_ids[0] if side == "UP" else market.token_ids[1]
    return AutoTradeSignal(
        side=side,
        token_id=token_id,
        price=quote.ask,
        reason=(
            f"protective_market_reversal primary_side={primary_side} entry={quote.ask} "
            f"opposite_bid={quote.bid} required_bid={reversal_bid_threshold} "
            f"seconds_left={int(seconds_to_end)}"
        ),
    )


def response_fill_amounts(
    response: dict[str, Any],
    fallback_price: Decimal,
    fallback_shares: Decimal,
) -> tuple[Decimal, Decimal]:
    try:
        cost = Decimal(str(response.get("makingAmount") or "0"))
        shares = Decimal(str(response.get("takingAmount") or "0"))
    except (ArithmeticError, ValueError):
        cost = Decimal("0")
        shares = Decimal("0")
    if shares <= 0:
        shares = fallback_shares
    if cost <= 0:
        cost = fallback_price * shares
    return cost, shares


def evaluate_protective_hedge_risk(
    primary_side: str,
    primary_cost: Decimal,
    primary_shares: Decimal,
    hedge_price: Decimal,
    hedge_shares: Decimal,
    fee_rate: Decimal,
) -> HedgeRiskEvaluation:
    if (
        primary_side not in {"UP", "DOWN"}
        or primary_cost <= 0
        or primary_shares <= 0
        or hedge_price <= 0
        or hedge_price > 1
        or hedge_shares <= 0
        or fee_rate < 0
    ):
        return HedgeRiskEvaluation(False, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
    primary_price = primary_cost / primary_shares
    primary_fee = primary_shares * fee_rate * primary_price * (Decimal("1") - primary_price)
    hedge_cost = hedge_price * hedge_shares
    hedge_fee = hedge_shares * fee_rate * hedge_price * (Decimal("1") - hedge_price)
    total_cost = primary_cost + primary_fee + hedge_cost + hedge_fee
    primary_up_shares = primary_shares if primary_side == "UP" else Decimal("0")
    primary_down_shares = primary_shares if primary_side == "DOWN" else Decimal("0")
    hedge_up_shares = hedge_shares if primary_side == "DOWN" else Decimal("0")
    hedge_down_shares = hedge_shares if primary_side == "UP" else Decimal("0")
    pnl_up_after = primary_up_shares + hedge_up_shares - total_cost
    pnl_down_after = primary_down_shares + hedge_down_shares - total_cost
    max_loss_before = primary_cost + primary_fee
    max_loss_after = max(Decimal("0"), -min(pnl_up_after, pnl_down_after))
    return HedgeRiskEvaluation(
        reduces_max_loss=max_loss_after < max_loss_before,
        max_loss_before=max_loss_before,
        max_loss_after=max_loss_after,
        pnl_up_after=pnl_up_after,
        pnl_down_after=pnl_down_after,
    )


def choose_late_favorite_signal(
    market: Market,
    probability_up: Decimal,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    recent_spot_prices: list[Decimal],
    start_price: Decimal,
    entry_start_seconds: Decimal = Decimal("30"),
    entry_cutoff_seconds: Decimal = Decimal("12"),
    min_entry: Decimal = Decimal("0.70"),
    max_entry: Decimal = Decimal("0.84"),
    min_win_probability: Decimal = Decimal("0.82"),
    edge_margin: Decimal = Decimal("0.00"),
    fee_rate: Decimal = Decimal("0.07"),
    max_spread: Decimal = Decimal("0.02"),
    min_ask_sum: Decimal = Decimal("0.97"),
    max_ask_sum: Decimal = Decimal("1.03"),
    confirmation_samples: int = 4,
    min_expected_roi: Decimal = Decimal("0.08"),
    min_lead_bps: Decimal = Decimal("3.0"),
    max_pullback_bps: Decimal = Decimal("0.75"),
    no_cross_samples: int = 6,
    max_pullback_ratio: Decimal = Decimal("0.25"),
    sigma_per_sqrt_second: Decimal = Decimal("0"),
    volatility_buffer_multiplier: Decimal = Decimal("1.5"),
) -> AutoTradeSignal | None:
    if not entry_cutoff_seconds < seconds_to_end <= entry_start_seconds:
        return None
    ok, _ = quotes_pass_sanity_checks(
        up_quote,
        down_quote,
        max_spread,
        min_ask_sum,
        max_ask_sum,
    )
    if not ok:
        return None
    assert up_quote is not None and up_quote.ask is not None
    assert down_quote is not None and down_quote.ask is not None

    up_midpoint = (up_quote.bid + up_quote.ask) / Decimal("2")
    down_midpoint = (down_quote.bid + down_quote.ask) / Decimal("2")
    if up_midpoint == down_midpoint:
        return None
    if up_midpoint > down_midpoint:
        side = "UP"
        token_id = market.token_ids[0]
        entry = up_quote.ask
        probability = probability_up
        midpoint = up_midpoint
    else:
        side = "DOWN"
        token_id = market.token_ids[1]
        entry = down_quote.ask
        probability = Decimal("1") - probability_up
        midpoint = down_midpoint

    if entry < min_entry or entry > max_entry:
        return None
    buffer_metrics = late_spot_safety_metrics(
        recent_spot_prices,
        start_price,
        side,
        confirmation_samples,
        no_cross_samples,
    )
    if buffer_metrics is None:
        return None
    lead_bps, pullback_bps, pullback_ratio = buffer_metrics
    volatility_buffer_bps = (
        sigma_per_sqrt_second
        * Decimal(str(math.sqrt(float(max(Decimal("0"), seconds_to_end)))))
        * Decimal("10000")
        * volatility_buffer_multiplier
    )
    required_lead_bps = max(min_lead_bps, volatility_buffer_bps)
    if (
        lead_bps < required_lead_bps
        or pullback_bps > max_pullback_bps
        or pullback_ratio > max_pullback_ratio
    ):
        return None
    fee_per_share = fee_rate * entry * (Decimal("1") - entry)
    fee_per_stake = fee_rate * (Decimal("1") - entry)
    required_probability = max(
        min_win_probability,
        entry + fee_per_share + edge_margin,
        entry * (Decimal("1") + fee_per_stake + min_expected_roi),
    )
    if probability < required_probability:
        return None
    expected_roi = probability / entry - Decimal("1") - fee_per_stake

    return AutoTradeSignal(
        side=side,
        token_id=token_id,
        price=entry,
        reason=(
            f"late_favorite entry={entry} probability={probability.quantize(Decimal('0.0001'))} "
            f"required_probability={required_probability.quantize(Decimal('0.0001'))} "
            f"expected_roi={expected_roi.quantize(Decimal('0.0001'))} "
            f"fee_per_share={fee_per_share.quantize(Decimal('0.0001'))} "
            f"midpoint={midpoint.quantize(Decimal('0.0001'))} "
            f"lead_bps={lead_bps.quantize(Decimal('0.01'))} "
            f"required_lead_bps={required_lead_bps.quantize(Decimal('0.01'))} "
            f"volatility_buffer_bps={volatility_buffer_bps.quantize(Decimal('0.01'))} "
            f"pullback_bps={pullback_bps.quantize(Decimal('0.01'))} "
            f"pullback_ratio={pullback_ratio.quantize(Decimal('0.001'))} "
            f"seconds_left={int(seconds_to_end)}"
        ),
    )


def live_response_is_matched(response: Any) -> bool:
    return (
        isinstance(response, dict)
        and response.get("success") is True
        and str(response.get("status", "")).lower() == "matched"
        and bool(response.get("orderID"))
    )


def live_order_limit_reached(submitted: int, maximum: int) -> bool:
    return maximum > 0 and submitted >= maximum


def live_session_should_continue(attempts: int, maximum: int) -> bool:
    return not live_order_limit_reached(attempts, maximum)


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
    fee_rate: Decimal = Decimal("0"),
) -> Decimal:
    if stake <= 0:
        raise ValueError("--paper-stake must be positive")
    if signal.price <= 0:
        raise ValueError("Cannot paper trade at non-positive price")
    shares = stake / signal.price
    fee = shares * fee_rate * signal.price * (Decimal("1") - signal.price)
    total_cost = stake + fee
    if bankroll < total_cost:
        logger.info(
            "PAPER_SKIP insufficient bankroll=%s stake=%s fee=%s",
            bankroll,
            stake,
            fee,
        )
        return bankroll
    positions.append(
        PaperPosition(
            slug=market_slug,
            side=signal.side,
            entry_price=signal.price,
            stake=stake,
            shares=shares,
            fee=fee,
        )
    )
    bankroll -= total_cost
    logger.info(
        "PAPER_OPEN slug=%s side=%s entry=%s stake=%s fee=%s shares=%s bankroll=%s",
        market_slug,
        signal.side,
        signal.price,
        stake,
        fee.quantize(Decimal("0.0001")),
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
        profit = payout - position.stake - position.fee
        position.profit = profit
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


def account_new_paper_settlements(
    positions: list[PaperPosition],
    consecutive_losses: int,
    max_consecutive_losses: int,
    pause_windows_after_losses: int,
) -> tuple[int, int]:
    pause_windows = 0
    for position in positions:
        if not position.settled or position.accounted or position.profit is None:
            continue
        position.accounted = True
        if max_consecutive_losses <= 0:
            consecutive_losses = 0
            continue
        if position.profit < 0:
            consecutive_losses += 1
        else:
            consecutive_losses = 0
        if max_consecutive_losses > 0 and consecutive_losses >= max_consecutive_losses:
            pause_windows = max(pause_windows, pause_windows_after_losses)
            logger.info(
                "RISK_PAUSE_TRIGGER consecutive_losses=%s pause_windows=%s",
                consecutive_losses,
                pause_windows,
            )
            consecutive_losses = 0
    return consecutive_losses, pause_windows


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


def polling_interval_for_seconds_left(
    seconds_to_end: Decimal,
    normal_interval: float,
    final_poll_seconds: int,
    final_poll_interval: float,
) -> float:
    if final_poll_seconds > 0 and Decimal("0") < seconds_to_end <= Decimal(final_poll_seconds):
        return min(normal_interval, final_poll_interval)
    return normal_interval


def sleep_until_next_poll(interval: float, iteration_started_at: float) -> None:
    remaining = interval - (time.monotonic() - iteration_started_at)
    time.sleep(max(0.05, remaining))


def watch() -> None:
    global _ACTIVE_NOTIFICATIONS

    load_dotenv()
    env_parser = argparse.ArgumentParser(add_help=False)
    env_parser.add_argument("--env-file")
    env_args, _ = env_parser.parse_known_args()
    if env_args.env_file:
        load_dotenv(env_args.env_file, override=True)
    args = parse_args()
    gamma = GammaClient()
    clob = ClobDataClient(args.clob_host, timeout=args.market_data_timeout)
    trader: ClobTradingClient | None = None
    price_client = SpotPriceClient(args.price_source, timeout=args.market_data_timeout, ws_proxy=args.ws_proxy)
    price_to_beat_client = PolymarketPriceToBeatClient(
        timeout=args.market_data_timeout,
        proxy_url=args.price_to_beat_proxy or args.ws_proxy,
    )
    snapshot_writer = JsonlSnapshotWriter(Path(args.record_jsonl)) if args.record_jsonl else None
    slug = slug_from_value(args.slug)
    stop_at = float("inf") if args.duration == 0 else time.time() + args.duration
    current_market: Market | None = None
    start_price: Decimal | None = None
    last_spot_price: Decimal | None = None
    last_spot_fetched_at: float | None = None
    prices: list[Decimal] = []
    signals_this_window = 0
    candidate_side: str | None = None
    candidate_confirmations = 0
    market_reversal_candidate_side: str | None = None
    market_reversal_confirmations = 0
    primary_side_this_window: str | None = None
    primary_cost_this_window = Decimal("0")
    primary_shares_this_window = Decimal("0")
    edge_threshold = Decimal(args.edge)
    fallback_sigma = Decimal(args.fallback_sigma)
    confirmation_jump_sigma_multiplier = Decimal(args.confirmation_jump_sigma_multiplier)
    confirmation_min_jump_usd = Decimal(args.confirmation_min_jump_usd)
    hedge_min_win_probability = Decimal(args.hedge_min_win_probability)
    hedge_fee_rate = Decimal(args.hedge_fee_rate)
    hedge_entry_start_seconds = Decimal(str(args.hedge_entry_start_seconds))
    hedge_entry_cutoff_seconds = Decimal(str(args.hedge_entry_cutoff_seconds))
    hedge_market_reversal_threshold = Decimal(args.hedge_market_reversal_threshold)
    hedge_max_entry = Decimal(args.hedge_max_entry)
    hedge_max_live_notional = Decimal(args.hedge_max_live_notional)
    min_entry = Decimal(args.min_entry)
    max_entry = Decimal(args.max_entry)
    max_spread = Decimal(args.max_spread)
    min_ask_sum = Decimal(args.min_ask_sum)
    max_ask_sum = Decimal(args.max_ask_sum)
    min_win_probability = Decimal(args.min_win_probability)
    probability_shrinkage = Decimal(args.probability_shrinkage)
    low_entry_cutoff = Decimal(args.low_entry_cutoff)
    low_entry_min_win_probability = Decimal(args.low_entry_min_win_probability)
    max_price_alignment_difference = Decimal(args.max_price_alignment_difference)
    late_min_entry = Decimal(args.late_min_entry)
    late_max_entry = Decimal(args.late_max_entry)
    late_min_win_probability = Decimal(args.late_min_win_probability)
    late_edge_margin = Decimal(args.late_edge_margin)
    late_min_expected_roi = Decimal(args.late_min_expected_roi)
    late_fee_rate = Decimal(args.late_fee_rate)
    late_max_spread = Decimal(args.late_max_spread)
    late_min_ask_sum = Decimal(args.late_min_ask_sum)
    late_max_ask_sum = Decimal(args.late_max_ask_sum)
    late_min_lead_bps = Decimal(args.late_min_lead_bps)
    late_max_pullback_bps = Decimal(args.late_max_pullback_bps)
    late_max_pullback_ratio = Decimal(args.late_max_pullback_ratio)
    late_volatility_buffer_multiplier = Decimal(args.late_volatility_buffer_multiplier)
    order_size = Decimal(args.order_size)
    max_live_notional = Decimal(args.max_live_notional)
    late_max_live_notional = Decimal(args.late_max_live_notional)
    decision_seconds_before_end = Decimal(str(args.decision_seconds_before_end))
    min_seconds_before_end = Decimal(str(args.min_seconds_before_end))
    paper_bankroll = Decimal(args.paper_bankroll)
    paper_stake = Decimal(args.paper_stake)
    paper_positions: list[PaperPosition] = []
    consecutive_losses = 0
    pause_windows_remaining = 0
    risk_pause_active_for_window = False
    live_orders_submitted = 0
    live_orders_matched = 0
    live_summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "mode": "live" if args.live_trading else "paper" if args.paper_trading else "dry_run",
        "status": "running",
        "strategy": args.strategy,
        "max_live_orders": args.max_live_orders,
        "max_trades_per_window": args.max_trades,
        "max_live_notional": str(max_live_notional),
        "hedge_max_live_notional": str(hedge_max_live_notional),
        "late_max_live_notional": str(late_max_live_notional),
        "probability_shrinkage": str(probability_shrinkage),
        "order_attempts": 0,
        "matched_orders": 0,
        "order": None,
        "response": None,
        "orders": [],
        "error": None,
    }

    def write_live_summary(finalize: bool = True) -> None:
        if not args.live_trading:
            return
        if finalize:
            if live_summary["status"] == "running":
                live_summary["status"] = (
                    "ended_after_orders" if live_summary["order_attempts"] else "ended_without_order"
                )
            live_summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        path = Path(args.live_summary_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(live_summary, ensure_ascii=True, indent=2, default=str) + "\n")

    if args.live_trading:
        atexit.register(write_live_summary)

    notifications = TradingNotificationService.from_env(
        trader=None,
        signature_type=args.signature_type,
        strategy=args.strategy,
        mode=str(live_summary["mode"]),
        version=__version__,
        summary=live_summary,
        wallet_address=os.getenv(args.funder_address_env) or os.getenv("DEPOSIT_WALLET"),
    )
    args.strategy = notifications.resolve_effective_strategy()
    live_summary["strategy"] = args.strategy
    _ACTIVE_NOTIFICATIONS = notifications
    atexit.register(notifications.stop, "进程退出")

    try:
        if args.live_trading and not args.auto_trade:
            raise ValueError("--live-trading requires --auto-trade")
        if args.duration < 0:
            raise ValueError("--duration must be zero (unlimited) or positive")
        if args.live_trading and args.strategy not in LIVE_STRATEGIES:
            raise ValueError(f"{args.strategy} is not approved for live strategy selection")
        if args.live_trading and (
            args.max_live_orders < 0
            or args.max_trades < 1
            or order_size <= 0
            or max_live_notional <= 0
            or late_max_live_notional <= 0
        ):
            raise ValueError(
                "Live order size, per-window limit, and notional must be positive; session limit may be zero"
            )
        if not Decimal("0") <= low_entry_cutoff <= max_entry:
            raise ValueError("Low-entry cutoff must be between zero and maximum entry")
        if not Decimal("0") <= low_entry_min_win_probability <= Decimal("1"):
            raise ValueError("Low-entry minimum win probability must be between zero and one")
        if not Decimal("0") <= probability_shrinkage <= Decimal("1"):
            raise ValueError("Probability shrinkage must be between zero and one")
        if fallback_sigma <= 0:
            raise ValueError("Fallback sigma must be positive")
        if args.trend_confirmation_samples < 1:
            raise ValueError("Trend confirmation samples must be positive")
        if args.hedge_signal_confirmations < 1:
            raise ValueError("Hedge signal confirmations must be positive")
        if args.hedge_market_reversal_confirmations < 1:
            raise ValueError("Hedge market-reversal confirmations must be positive")
        if hedge_entry_cutoff_seconds < 0 or hedge_entry_cutoff_seconds >= hedge_entry_start_seconds:
            raise ValueError("Hedge entry cutoff must be non-negative and lower than its start")
        if not Decimal("0.5") < hedge_market_reversal_threshold < Decimal("1"):
            raise ValueError("Hedge market-reversal threshold must be between 0.5 and 1")
        if not Decimal("0") < hedge_max_entry < Decimal("1"):
            raise ValueError("Hedge maximum entry must be between zero and one")
        if hedge_max_live_notional <= 0:
            raise ValueError("Hedge maximum live notional must be positive")
        if args.final_poll_seconds < 0 or args.final_poll_interval <= 0:
            raise ValueError("Final polling window must be non-negative and interval positive")
        if not Decimal("0") <= hedge_min_win_probability <= Decimal("1"):
            raise ValueError("Hedge minimum win probability must be between zero and one")
        if hedge_fee_rate < 0:
            raise ValueError("Hedge fee rate must be non-negative")
        if confirmation_jump_sigma_multiplier < 0 or confirmation_min_jump_usd < 0:
            raise ValueError("Confirmation jump thresholds must be non-negative")
        if args.low_entry_confirmation_samples < 1:
            raise ValueError("Low-entry confirmation samples must be positive")
        if max_price_alignment_difference < 0:
            raise ValueError("Maximum price-alignment difference must be non-negative")
        if args.max_boundary_sample_offset_ms < 0:
            raise ValueError("Maximum boundary-sample offset must be non-negative")
        if args.late_entry_cutoff_seconds >= args.late_entry_start_seconds:
            raise ValueError("late_favorite entry cutoff must be lower than entry start")
        if not Decimal("0") < late_min_entry <= late_max_entry < Decimal("1"):
            raise ValueError("late_favorite entry range must be within zero and one")
        if not Decimal("0") <= late_min_win_probability <= Decimal("1"):
            raise ValueError("late_favorite minimum win probability must be between zero and one")
        if (
            late_edge_margin < 0
            or late_min_expected_roi < 0
            or late_fee_rate < 0
            or late_max_spread < 0
            or late_min_lead_bps < 0
            or late_max_pullback_bps < 0
            or late_volatility_buffer_multiplier < 0
        ):
            raise ValueError("late_favorite risk thresholds must not be negative")
        if not Decimal("0") <= late_max_pullback_ratio <= Decimal("1"):
            raise ValueError("late_favorite pullback ratio must be between zero and one")
        if (
            args.late_confirmation_samples < 1
            or args.late_no_cross_samples < args.late_confirmation_samples
            or args.late_signal_confirmations < 1
            or args.late_pause_windows_after_loss < 0
        ):
            raise ValueError("late_favorite confirmations must be positive and pause must not be negative")
        trader = build_live_trader(args)
        notifications.trader = trader
    except Exception as exc:
        live_summary["error"] = f"{type(exc).__name__}: {exc}"
        notifications.notify_exception("启动检查或钱包签名", exc, key="startup", cooldown=0)
        notifications.stop("启动失败", exc)
        raise

    notifications.start()
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

    while time.time() < stop_at:
        iteration_started_at = time.monotonic()
        poll_interval = float(args.interval)
        now = datetime.now(timezone.utc)
        notifications.update_runtime()
        if notifications.process_commands():
            live_summary["status"] = "restarting"
            write_live_summary()
            notifications.prepare_restart()
            os.execv(
                sys.executable,
                [sys.executable, "-m", "src.watch_updown", *sys.argv[1:]],
            )
        notifications.maybe_send_settlements(fetch_winner)
        notifications.maybe_send_daily(fetch_winner)
        if args.paper_trading:
            paper_bankroll = settle_all_paper_positions(paper_positions, paper_bankroll)
            if args.strategy == "late_favorite":
                loss_pause = args.late_pause_windows_after_loss
                loss_limit = 1 if loss_pause > 0 else 0
            else:
                loss_pause = args.pause_windows_after_losses
                loss_limit = args.max_consecutive_losses
            consecutive_losses, new_pause_windows = account_new_paper_settlements(
                paper_positions,
                consecutive_losses,
                loss_limit,
                loss_pause,
            )
            pause_windows_remaining = max(pause_windows_remaining, new_pause_windows)
            if (
                args.stop_when_bust
                and paper_bankroll <= 0
                and all(position.settled for position in paper_positions)
            ):
                logger.info("PAPER_BUST bankroll=%s. Exiting.", paper_bankroll)
                return

        if current_market is None or _seconds_to_end(current_market, now) <= 0:
            if current_market is not None:
                slug = next_5m_slug(current_market.slug)
                logger.info("Window ended. Looking for next slug: %s", slug)
            current_market = load_updown_market(gamma, slug)
            start_price = None
            prices = []
            signals_this_window = 0
            candidate_side = None
            candidate_confirmations = 0
            market_reversal_candidate_side = None
            market_reversal_confirmations = 0
            primary_side_this_window = None
            primary_cost_this_window = Decimal("0")
            primary_shares_this_window = Decimal("0")
            if current_market is None:
                notifications.notify_exception(
                    "读取 Polymarket 市场",
                    RuntimeError(f"暂时无法读取市场 {slug}"),
                    key=f"market:{slug}",
                )
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            if _seconds_to_end(current_market, datetime.now(timezone.utc)) <= 0:
                logger.info("Skipping expired window: %s", current_market.slug)
                slug = next_5m_slug(current_market.slug)
                current_market = None
                continue
            risk_pause_active_for_window, pause_windows_remaining = consume_pause_window(
                pause_windows_remaining
            )
            if risk_pause_active_for_window:
                logger.info(
                    "RISK_PAUSE_ACTIVE remaining_windows_after_this=%s",
                    pause_windows_remaining,
                )
            activated_strategy = notifications.activate_pending_strategy(current_market.slug)
            if activated_strategy is not None:
                args.strategy = activated_strategy
                live_summary["strategy"] = activated_strategy
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
        poll_interval = polling_interval_for_seconds_left(
            seconds_to_end,
            float(args.interval),
            args.final_poll_seconds,
            args.final_poll_interval,
        )
        try:
            spot = price_client.btc_usd()
            if spot.observed_at is not None:
                report_age = abs(int(time.time()) - spot.observed_at)
                if report_age > args.max_spot_age:
                    raise RuntimeError(f"Chainlink report is stale by {report_age}s")
            last_spot_price = spot.price
            last_spot_fetched_at = time.monotonic()
        except Exception as exc:
            notifications.notify_exception("Chainlink BTC 行情", exc, key="spot-price")
            if last_spot_price is None:
                logger.warning("Spot price unavailable and no cached price exists: %s", exc)
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            logger.warning("Spot price unavailable; reusing cached BTC/USD=%s: %s", last_spot_price, exc)
            spot = type("CachedSpotPrice", (), {"price": last_spot_price, "source": "CACHE"})()

        spot_age = Decimal(str(time.monotonic() - last_spot_fetched_at)) if last_spot_fetched_at is not None else Decimal("Infinity")
        notifications.update_runtime(
            slug=current_market.slug,
            seconds_left=seconds_to_end,
            spot=spot.price,
            spot_source=spot.source,
        )

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
            elapsed_since_start = -seconds_to_start
            try:
                price_to_beat = price_to_beat_client.fetch(
                    current_market.event_start_time,
                    current_market.end_time,
                )
            except Exception as exc:
                logger.warning(
                    "PRICE_ALIGNMENT_PENDING slug=%s error=%s",
                    current_market.slug,
                    exc,
                )
                if official_open_retry_expired(
                    seconds_to_end,
                    args.strategy,
                    decision_seconds_before_end,
                    Decimal(str(args.late_entry_start_seconds)),
                ):
                    logger.warning(
                        "PRICE_ALIGNMENT_UNAVAILABLE slug=%s seconds_left=%s "
                        "entry_phase_started=true; skipping window",
                        current_market.slug,
                        int(seconds_to_end),
                    )
                    slug = next_5m_slug(current_market.slug)
                    current_market = None
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            else:
                boundary_timestamp_ms = int(current_market.event_start_time.timestamp() * 1000)
                boundary_spot = None
                boundary_error: str | None = None
                try:
                    boundary_spot = price_client.polymarket_chainlink_price_near(
                        boundary_timestamp_ms,
                        args.max_boundary_sample_offset_ms,
                    )
                except Exception as exc:
                    boundary_error = str(exc)
                    logger.warning(
                        "PRICE_ALIGNMENT_UNVERIFIED slug=%s reason=boundary_sample error=%s; "
                        "using official openPrice",
                        current_market.slug,
                        exc,
                    )
                alignment_status, alignment_difference = price_alignment_status(
                    price_to_beat.open_price,
                    boundary_spot.price if boundary_spot is not None else None,
                    max_price_alignment_difference,
                )
                boundary_offset_ms = (
                    boundary_spot.observed_at_ms - boundary_timestamp_ms
                    if boundary_spot is not None and boundary_spot.observed_at_ms is not None
                    else None
                )
                if alignment_status == "MISMATCH_WARNING":
                    logger.warning(
                        "PRICE_ALIGNMENT_MISMATCH slug=%s official=%s boundary_spot=%s "
                        "difference=%s max_difference=%s boundary_offset_ms=%s; "
                        "using official openPrice",
                        current_market.slug,
                        price_to_beat.open_price,
                        boundary_spot.price if boundary_spot is not None else None,
                        alignment_difference,
                        max_price_alignment_difference,
                        boundary_offset_ms,
                    )
                start_price = price_to_beat.open_price
                alignment_record = {
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "slug": current_market.slug,
                    "status": alignment_status,
                    "official_price_to_beat": str(start_price),
                    "boundary_chainlink_price": (
                        str(boundary_spot.price) if boundary_spot is not None else None
                    ),
                    "boundary_chainlink_timestamp_ms": (
                        boundary_spot.observed_at_ms if boundary_spot is not None else None
                    ),
                    "boundary_offset_ms": boundary_offset_ms,
                    "alignment_difference": (
                        str(alignment_difference) if alignment_difference is not None else None
                    ),
                    "boundary_error": boundary_error,
                    "capture_delay_seconds": str(elapsed_since_start),
                    "endpoint_timestamp_ms": price_to_beat.timestamp_ms,
                    "endpoint_incomplete": price_to_beat.incomplete,
                }
                logger.info(
                    "PRICE_ALIGNMENT %s slug=%s official=%s boundary_spot=%s "
                    "difference=%s boundary_offset_ms=%s capture_delay=%ss",
                    alignment_status,
                    current_market.slug,
                    start_price,
                    boundary_spot.price if boundary_spot is not None else None,
                    alignment_difference,
                    boundary_offset_ms,
                    elapsed_since_start,
                )
                if args.price_alignment_jsonl:
                    alignment_path = Path(args.price_alignment_jsonl)
                    alignment_path.parent.mkdir(parents=True, exist_ok=True)
                    with alignment_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(alignment_record, ensure_ascii=True) + "\n")
            prices = [spot.price]
            logger.info("Captured start_price=%s for %s", start_price, current_market.slug)
        else:
            prices.append(spot.price)

        sigma = estimate_sigma_per_sqrt_second(prices, Decimal(str(poll_interval)), fallback_sigma)
        fair = btc_up_probability(start_price, spot.price, max(Decimal("0"), seconds_to_end), sigma)
        up_quote, down_quote = quote_outcomes(clob, current_market)
        up_ask = up_quote.ask if up_quote else None
        down_ask = down_quote.ask if down_quote else None
        action = choose_theoretical_action(fair.probability_up, up_ask, down_ask, edge_threshold)

        if snapshot_writer is not None:
            snapshot_writer.write(
                build_snapshot(
                    observed_at=now.isoformat(),
                    observed_ts=int(now.timestamp()),
                    slug=current_market.slug,
                    market_start_ts=int(current_market.event_start_time.timestamp()),
                    market_end_ts=int(current_market.end_time.timestamp()),
                    seconds_left=seconds_to_end,
                    spot=spot.price,
                    start_spot=start_price,
                    spot_source=spot.source,
                    probability_up=fair.probability_up,
                    up_quote=up_quote,
                    down_quote=down_quote,
                )
            )

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

        if (
            args.auto_trade
            and signals_this_window < strategy_trade_limit(args.strategy, args.max_trades)
            and not risk_pause_active_for_window
            and not notifications.trading_paused
        ):
            if args.strategy == "late_favorite":
                signal = choose_late_favorite_signal(
                    current_market,
                    fair.probability_up,
                    up_quote,
                    down_quote,
                    seconds_to_end,
                    prices,
                    start_price,
                    Decimal(str(args.late_entry_start_seconds)),
                    Decimal(str(args.late_entry_cutoff_seconds)),
                    late_min_entry,
                    late_max_entry,
                    late_min_win_probability,
                    late_edge_margin,
                    late_fee_rate,
                    late_max_spread,
                    late_min_ask_sum,
                    late_max_ask_sum,
                    args.late_confirmation_samples,
                    late_min_expected_roi,
                    late_min_lead_bps,
                    late_max_pullback_bps,
                    args.late_no_cross_samples,
                    late_max_pullback_ratio,
                    fair.sigma_per_sqrt_second,
                    late_volatility_buffer_multiplier,
                )
            else:
                signal = choose_fair_value_edge_signal(
                    current_market,
                    fair.probability_up,
                    up_quote,
                    down_quote,
                    seconds_to_end,
                    decision_seconds_before_end,
                    min_entry,
                    max_entry,
                    edge_threshold,
                    max_spread,
                    min_ask_sum,
                    max_ask_sum,
                    min_seconds_before_end,
                    min_win_probability,
                    prices,
                    start_price,
                    low_entry_cutoff,
                    low_entry_min_win_probability,
                    args.low_entry_confirmation_samples,
                    probability_shrinkage,
                )
                if primary_side_this_window is not None and (
                    signal is None or signal.side != primary_side_this_window
                ):
                    model_protective_signal = choose_protective_hedge_signal(
                        current_market,
                        primary_side_this_window,
                        fair.probability_up,
                        up_quote,
                        down_quote,
                        seconds_to_end,
                        hedge_entry_start_seconds,
                        hedge_entry_cutoff_seconds,
                        hedge_max_entry,
                        edge_threshold,
                        hedge_min_win_probability,
                        max_spread,
                        min_ask_sum,
                        max_ask_sum,
                    )
                    market_protective_signal = choose_market_reversal_hedge_signal(
                        current_market,
                        primary_side_this_window,
                        up_quote,
                        down_quote,
                        seconds_to_end,
                        hedge_entry_start_seconds,
                        hedge_entry_cutoff_seconds,
                        hedge_market_reversal_threshold,
                        hedge_max_entry,
                        max_spread,
                        min_ask_sum,
                        max_ask_sum,
                    )
                    if market_protective_signal is None:
                        market_reversal_candidate_side = None
                        market_reversal_confirmations = 0
                    elif market_protective_signal.side == market_reversal_candidate_side:
                        market_reversal_confirmations += 1
                    else:
                        market_reversal_candidate_side = market_protective_signal.side
                        market_reversal_confirmations = 1
                    if market_protective_signal is not None and (
                        market_reversal_confirmations
                        < args.hedge_market_reversal_confirmations
                    ):
                        logger.info(
                            "MARKET_REVERSAL_PENDING side=%s confirmations=%s/%s bid_threshold=%s",
                            market_protective_signal.side,
                            market_reversal_confirmations,
                            args.hedge_market_reversal_confirmations,
                            hedge_market_reversal_threshold,
                        )
                        market_protective_signal = None
                    elif market_protective_signal is not None:
                        market_reversal_candidate_side = None
                        market_reversal_confirmations = 0
                    signal = market_protective_signal or model_protective_signal
            if signal is not None:
                if notifications.trading_paused:
                    logger.warning("AUTO_SIGNAL blocked because Telegram trading pause is active.")
                    candidate_side = None
                    candidate_confirmations = 0
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                if spot_age > Decimal(str(args.max_spot_age)):
                    logger.info("SIGNAL_REJECTED stale_spot_age=%.1fs", float(spot_age))
                    candidate_side = None
                    candidate_confirmations = 0
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                is_protective_hedge = signal.reason.startswith("protective_")
                if (
                    args.strategy == "fair_value_edge"
                    and not is_protective_hedge
                    and not recent_spot_samples_support_side(
                        prices,
                        start_price,
                        signal.side,
                        args.trend_confirmation_samples,
                    )
                ):
                    logger.info(
                        "SIGNAL_REJECTED side=%s reason=recent_spot_distance_narrowing samples=%s",
                        signal.side,
                        args.trend_confirmation_samples,
                    )
                    candidate_side = None
                    candidate_confirmations = 0
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                jump_reset, adverse_jump, jump_threshold = adverse_jump_exceeds_dynamic_threshold(
                    prices,
                    signal.side,
                    fair.sigma_per_sqrt_second,
                    Decimal(str(poll_interval)),
                    confirmation_jump_sigma_multiplier,
                    confirmation_min_jump_usd,
                )
                if jump_reset:
                    logger.info(
                        "SIGNAL_CONFIRMATION_RESET side=%s adverse_jump=%s threshold=%s",
                        signal.side,
                        adverse_jump.quantize(Decimal("0.01")),
                        jump_threshold.quantize(Decimal("0.01")),
                    )
                    candidate_side = None
                    candidate_confirmations = 0
                required_confirmations = (
                    1
                    if signal.reason.startswith("protective_market_reversal")
                    else args.hedge_signal_confirmations
                    if is_protective_hedge
                    else args.late_signal_confirmations
                    if args.strategy == "late_favorite"
                    else args.signal_confirmations
                )
                if signal.side == candidate_side:
                    candidate_confirmations += 1
                else:
                    candidate_side = signal.side
                    candidate_confirmations = 1
                if candidate_confirmations < max(1, required_confirmations):
                    logger.info(
                        "SIGNAL_PENDING side=%s confirmations=%s/%s",
                        signal.side,
                        candidate_confirmations,
                        max(1, required_confirmations),
                    )
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                candidate_side = None
                candidate_confirmations = 0
                if is_protective_hedge:
                    hedge_risk = evaluate_protective_hedge_risk(
                        primary_side_this_window or "",
                        primary_cost_this_window,
                        primary_shares_this_window,
                        signal.price,
                        order_size,
                        hedge_fee_rate,
                    )
                    if not hedge_risk.reduces_max_loss:
                        logger.info(
                            "HEDGE_REJECTED slug=%s side=%s max_loss_before=%s max_loss_after=%s",
                            current_market.slug,
                            signal.side,
                            hedge_risk.max_loss_before.quantize(Decimal("0.0001")),
                            hedge_risk.max_loss_after.quantize(Decimal("0.0001")),
                        )
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    signal = AutoTradeSignal(
                        side=signal.side,
                        token_id=signal.token_id,
                        price=signal.price,
                        reason=(
                            f"{signal.reason} "
                            f"max_loss_before={hedge_risk.max_loss_before.quantize(Decimal('0.0001'))} "
                            f"max_loss_after={hedge_risk.max_loss_after.quantize(Decimal('0.0001'))}"
                        ),
                    )
                logger.info(
                    "AUTO_SIGNAL %s side=%s price=%s size=%s reason=%s",
                    current_market.slug,
                    signal.side,
                    signal.price,
                    order_size,
                    signal.reason,
                )
                if trader is None:
                    signals_this_window = window_trade_count_after_attempt(
                        signals_this_window,
                        live=False,
                    )
                    if args.paper_trading:
                        paper_fee_rate = late_fee_rate if args.strategy == "late_favorite" else Decimal("0")
                        paper_bankroll = open_paper_position(
                            paper_positions,
                            paper_bankroll,
                            current_market.slug,
                            signal,
                            paper_stake,
                            paper_fee_rate,
                        )
                        if args.stop_when_bust and paper_bankroll <= 0:
                            logger.info("PAPER_BUST bankroll=%s. Exiting after open position.", paper_bankroll)
                            return
                    else:
                        logger.info("DRY RUN: would buy %s at %s x %s", signal.side, signal.price, order_size)
                    if primary_side_this_window is None:
                        primary_side_this_window = signal.side
                        if args.paper_trading:
                            primary_cost_this_window = paper_stake
                            primary_shares_this_window = paper_stake / signal.price
                        else:
                            primary_cost_this_window = signal.price * order_size
                            primary_shares_this_window = order_size
                else:
                    notional = signal.price * order_size
                    live_notional_cap = (
                        hedge_max_live_notional
                        if is_protective_hedge
                        else late_max_live_notional
                        if args.strategy == "late_favorite"
                        else max_live_notional
                    )
                    if not live_session_should_continue(live_orders_submitted, args.max_live_orders):
                        logger.warning("LIVE ORDER LIMIT reached=%s. Exiting.", live_orders_submitted)
                        return
                    if notional > live_notional_cap:
                        logger.warning(
                            "LIVE SIGNAL SKIPPED notional=%s exceeds hard cap=%s; continuing.",
                            notional,
                            live_notional_cap,
                        )
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    if notifications.trading_paused:
                        logger.warning("LIVE ORDER blocked by Telegram trading pause before submission.")
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    order_record = {
                        "attempted_at": datetime.now(timezone.utc).isoformat(),
                        "slug": current_market.slug,
                        "side": signal.side,
                        "token_id": signal.token_id,
                        "price": str(signal.price),
                        "size": str(order_size),
                        "notional": str(notional),
                        "order_type": args.live_order_type,
                        "reason": signal.reason,
                        "response": None,
                        "error": None,
                    }
                    live_summary["status"] = "submitting"
                    live_summary["order_attempts"] = live_orders_submitted + 1
                    live_summary["order"] = {key: value for key, value in order_record.items() if key not in {"response", "error"}}
                    live_summary["orders"].append(order_record)
                    live_orders_submitted += 1
                    live_summary["order_attempts"] = live_orders_submitted
                    write_live_summary(finalize=False)
                    try:
                        response = trader.buy_limit(
                            token_id=signal.token_id,
                            price=signal.price,
                            size=order_size,
                            tick_size=current_market.minimum_tick_size,
                            neg_risk=current_market.neg_risk,
                            order_type=args.live_order_type,
                        )
                    except Exception as exc:
                        live_summary["status"] = "running"
                        live_summary["error"] = f"{type(exc).__name__}: {exc}"
                        order_record["error"] = live_summary["error"]
                        write_live_summary(finalize=False)
                        notifications.notify_exception(
                            f"提交订单 {current_market.slug} {signal.side}",
                            exc,
                            key=f"order:{current_market.slug}:{live_orders_submitted}",
                            cooldown=0,
                        )
                        logger.warning(
                            "LIVE ORDER attempt=%s raised %s; continuing.",
                            live_orders_submitted,
                            live_summary["error"],
                        )
                        if not live_session_should_continue(live_orders_submitted, args.max_live_orders):
                            live_summary["status"] = "completed"
                            write_live_summary()
                            return
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    live_summary["response"] = response
                    order_record["response"] = response
                    logger.info("LIVE ORDER response=%s", response)
                    matched = live_response_is_matched(response)
                    signals_this_window = window_trade_count_after_attempt(
                        signals_this_window,
                        live=True,
                        matched=matched,
                    )
                    if not matched:
                        live_summary["status"] = "running"
                        live_summary["error"] = f"Order response was not conclusively matched: {response}"
                        order_record["error"] = live_summary["error"]
                        write_live_summary(finalize=False)
                        notifications.notify_exception(
                            f"订单未成交 {current_market.slug} {signal.side}",
                            live_summary["error"],
                            key=f"unmatched:{current_market.slug}:{live_orders_submitted}",
                            cooldown=0,
                        )
                        logger.warning(
                            "LIVE ORDER attempt=%s was not matched; continuing.",
                            live_orders_submitted,
                        )
                        if not live_session_should_continue(live_orders_submitted, args.max_live_orders):
                            live_summary["status"] = "completed"
                            write_live_summary()
                            return
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    live_orders_matched += 1
                    if primary_side_this_window is None:
                        primary_side_this_window = signal.side
                        primary_cost_this_window, primary_shares_this_window = response_fill_amounts(
                            response,
                            signal.price,
                            order_size,
                        )
                    live_summary["matched_orders"] = live_orders_matched
                    order_record["matched_at"] = datetime.now(timezone.utc).isoformat()
                    notifications.record_fill(order_record)
                    if not live_session_should_continue(live_orders_submitted, args.max_live_orders):
                        live_summary["status"] = "completed"
                        write_live_summary()
                        logger.warning(
                            "LIVE SESSION COMPLETE after %s attempt(s), %s matched. Exiting.",
                            live_orders_submitted,
                            live_orders_matched,
                        )
                        return
                    live_summary["status"] = "running"
                    write_live_summary(finalize=False)
                    logger.warning(
                        "LIVE ORDER attempt=%s matched_count=%s. Continuing; session_limit=%s.",
                        live_orders_submitted,
                        live_orders_matched,
                        args.max_live_orders if args.max_live_orders > 0 else "unlimited",
                    )
            else:
                candidate_side = None
                candidate_confirmations = 0

        sleep_until_next_poll(poll_interval, iteration_started_at)

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
    try:
        watch()
    except KeyboardInterrupt:
        if _ACTIVE_NOTIFICATIONS is not None:
            _ACTIVE_NOTIFICATIONS.stop("手动停止")
    except BaseException as exc:
        if _ACTIVE_NOTIFICATIONS is not None:
            _ACTIVE_NOTIFICATIONS.stop("异常崩溃", exc)
        raise
    else:
        if _ACTIVE_NOTIFICATIONS is not None:
            _ACTIVE_NOTIFICATIONS.stop("正常退出")
