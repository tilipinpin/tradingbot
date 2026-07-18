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
from decimal import ROUND_CEILING, Decimal
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
    OrderBookLevel,
    OrderBookQuote,
    OrderBookSnapshot,
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
class BookFill:
    shares: Decimal
    cost: Decimal
    fee: Decimal
    vwap: Decimal
    worst_price: Decimal


@dataclass(frozen=True)
class HedgeRiskEvaluation:
    reduces_max_loss: bool
    max_loss_before: Decimal
    max_loss_after: Decimal
    pnl_up_after: Decimal
    pnl_down_after: Decimal


@dataclass(frozen=True)
class SplitMakerQuotePlan:
    up_price: Decimal
    down_price: Decimal


@dataclass
class SplitMakerCycle:
    slug: str
    shares: Decimal
    cash_flow: Decimal
    inventory_up: Decimal
    inventory_down: Decimal
    up_quote: Decimal | None = None
    down_quote: Decimal | None = None
    up_quote_started_at: float | None = None
    down_quote_started_at: float | None = None
    up_sold_price: Decimal | None = None
    down_sold_price: Decimal | None = None
    unpaired_since: float | None = None
    closed: bool = False
    close_reason: str | None = None


@dataclass(frozen=True)
class SplitMakerExit:
    action: str
    cash_delta: Decimal
    price: Decimal
    fee: Decimal


@dataclass
class MakerMomentumProbe:
    up_quote: Decimal | None = None
    down_quote: Decimal | None = None
    up_quote_started_at: float | None = None
    down_quote_started_at: float | None = None
    candidate_side: str | None = None
    trigger_price: Decimal | None = None
    candidate_started_at: float | None = None
    confirmations: int = 0


@dataclass(frozen=True)
class MakerMomentumEvaluation:
    signal: AutoTradeSignal | None
    rejection_reason: str | None = None


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
    parser.add_argument("--max-spot-age", type=int, default=20, help="Maximum cached spot-price age allowed for entries.")
    parser.add_argument("--max-start-capture-delay", type=int, default=15, help="Skip a window if its start price is captured later than this many seconds.")
    parser.add_argument(
        "--official-price-to-beat",
        action="store_true",
        help="Use Polymarket's published crypto openPrice instead of the first post-open spot sample.",
    )
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
        help="Reject a window when official openPrice differs from the boundary Chainlink sample by more than this many USD.",
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
    parser.add_argument("--low-entry-cutoff", default="0.50")
    parser.add_argument("--low-entry-min-win-probability", default="0.68")
    parser.add_argument("--low-entry-confirmation-samples", type=int, default=3)
    parser.add_argument("--min-entry", default="0.55")
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
    parser.add_argument("--maker-shares", default="5")
    parser.add_argument("--maker-target-pair-sum", default="1.02")
    parser.add_argument("--maker-fee-rate", default="0.07")
    parser.add_argument("--maker-start-delay-seconds", type=int, default=10)
    parser.add_argument("--maker-min-rest-seconds", type=int, default=4)
    parser.add_argument("--maker-reprice-ticks", type=int, default=2)
    parser.add_argument("--maker-unpaired-timeout-seconds", type=int, default=10)
    parser.add_argument("--maker-cancel-seconds", type=int, default=60)
    parser.add_argument("--maker-force-exit-seconds", type=int, default=45)
    parser.add_argument("--maker-max-inventory-loss-rate", default="0.01")
    parser.add_argument("--momentum-target-pair-sum", default="1.02")
    parser.add_argument("--momentum-start-delay-seconds", type=int, default=10)
    parser.add_argument("--momentum-min-rest-seconds", type=int, default=4)
    parser.add_argument("--momentum-reprice-ticks", type=int, default=2)
    parser.add_argument("--momentum-confirmation-samples", type=int, default=1)
    parser.add_argument("--momentum-trigger-timeout-seconds", type=int, default=8)
    parser.add_argument("--momentum-min-seconds-before-end", type=int, default=30)
    parser.add_argument("--momentum-min-entry", default="0.60")
    parser.add_argument("--momentum-max-entry", default="0.88")
    parser.add_argument("--momentum-min-probability", default="0.55")
    parser.add_argument("--momentum-flow-probability-boost", default="0.10")
    parser.add_argument("--momentum-min-expected-roi", default="0.03")
    parser.add_argument("--momentum-min-lead-bps", default="0.50")
    parser.add_argument("--momentum-strong-expected-roi", default="0.04")
    parser.add_argument("--momentum-strong-lead-bps", default="2.00")
    parser.add_argument("--momentum-spot-samples", type=int, default=3)
    parser.add_argument("--momentum-max-chase", default="0.06")
    parser.add_argument("--momentum-fee-rate", default="0.07")
    parser.add_argument("--momentum-max-spread", default="0.02")
    parser.add_argument("--momentum-min-ask-sum", default="0.97")
    parser.add_argument("--momentum-max-ask-sum", default="1.03")
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


def start_capture_is_too_late(seconds_to_start: Decimal, max_delay: Decimal) -> bool:
    return -seconds_to_start > max_delay


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


def outcome_books(
    clob: ClobDataClient,
    market: Market,
) -> tuple[OrderBookSnapshot | None, OrderBookSnapshot | None]:
    try:
        up_book, down_book = clob.books(market.token_ids)
    except (RequestException, LookupError, ValueError) as exc:
        logger.warning("Could not fetch outcome books for %s: %s", market.slug, exc)
        return None, None
    return up_book, down_book


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
    if strategy in {"late_favorite", "maker_momentum"}:
        return 1
    return configured_limit


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


def fill_ask_depth(
    levels: tuple[OrderBookLevel, ...],
    shares: Decimal,
    fee_rate: Decimal,
) -> BookFill | None:
    if shares <= 0 or fee_rate < 0:
        raise ValueError("Ask-depth shares must be positive and fee rate must not be negative")
    remaining = shares
    cost = Decimal("0")
    fee = Decimal("0")
    worst_price: Decimal | None = None
    for level in sorted(levels, key=lambda item: item.price):
        if level.price <= 0 or level.size <= 0:
            continue
        filled = min(remaining, level.size)
        cost += filled * level.price
        fee += filled * fee_rate * level.price * (Decimal("1") - level.price)
        remaining -= filled
        worst_price = level.price
        if remaining <= 0:
            break
    if remaining > 0 or worst_price is None:
        return None
    return BookFill(
        shares=shares,
        cost=cost,
        fee=fee,
        vwap=cost / shares,
        worst_price=worst_price,
    )


def fill_bid_depth(
    levels: tuple[OrderBookLevel, ...],
    shares: Decimal,
    fee_rate: Decimal,
) -> BookFill | None:
    if shares <= 0 or fee_rate < 0:
        raise ValueError("Maker exit shares must be positive and fee rate must not be negative")
    remaining = shares
    proceeds = Decimal("0")
    fee = Decimal("0")
    worst_price: Decimal | None = None
    for level in sorted(levels, key=lambda item: item.price, reverse=True):
        if level.price <= 0 or level.size <= 0:
            continue
        filled = min(remaining, level.size)
        proceeds += filled * level.price
        fee += filled * fee_rate * level.price * (Decimal("1") - level.price)
        remaining -= filled
        worst_price = level.price
        if remaining <= 0:
            break
    if remaining > 0 or worst_price is None:
        return None
    return BookFill(
        shares=shares,
        cost=proceeds,
        fee=fee,
        vwap=proceeds / shares,
        worst_price=worst_price,
    )


def round_up_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    if tick_size <= 0:
        raise ValueError("tick size must be positive")
    return (value / tick_size).to_integral_value(rounding=ROUND_CEILING) * tick_size


def plan_split_maker_quotes(
    probability_up: Decimal,
    up_book: OrderBookSnapshot,
    down_book: OrderBookSnapshot,
    target_pair_sum: Decimal,
    tick_size: Decimal,
) -> SplitMakerQuotePlan | None:
    if not Decimal("0") <= probability_up <= Decimal("1") or target_pair_sum <= Decimal("1"):
        return None
    margin = (target_pair_sum - Decimal("1")) / Decimal("2")
    up_price = round_up_to_tick(probability_up + margin, tick_size)
    down_price = round_up_to_tick(Decimal("1") - probability_up + margin, tick_size)
    up_bid = up_book.quote.bid
    down_bid = down_book.quote.bid
    if up_bid is not None:
        up_price = max(up_price, round_up_to_tick(up_bid + tick_size, tick_size))
    if down_bid is not None:
        down_price = max(down_price, round_up_to_tick(down_bid + tick_size, tick_size))
    if up_price >= Decimal("1") or down_price >= Decimal("1"):
        return None
    if up_price + down_price < target_pair_sum:
        return None
    return SplitMakerQuotePlan(up_price=up_price, down_price=down_price)


def maker_quote_crossed(
    book: OrderBookSnapshot,
    quote_price: Decimal | None,
    quote_started_at: float | None,
    now_monotonic: float,
    min_rest_seconds: int,
) -> bool:
    if quote_price is None or quote_started_at is None:
        return False
    if now_monotonic - quote_started_at < max(0, min_rest_seconds):
        return False
    best_bid = book.quote.bid
    return best_bid is not None and best_bid >= quote_price


def evaluate_maker_momentum_signal(
    market: Market,
    side: str,
    trigger_price: Decimal,
    probability_up: Decimal,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    recent_spot_prices: list[Decimal],
    start_price: Decimal,
    min_seconds_before_end: Decimal = Decimal("30"),
    min_entry: Decimal = Decimal("0.60"),
    max_entry: Decimal = Decimal("0.85"),
    min_probability: Decimal = Decimal("0.55"),
    flow_probability_boost: Decimal = Decimal("0.10"),
    min_expected_roi: Decimal = Decimal("0.03"),
    min_lead_bps: Decimal = Decimal("0.50"),
    spot_samples: int = 3,
    max_chase: Decimal = Decimal("0.06"),
    fee_rate: Decimal = Decimal("0.07"),
    max_spread: Decimal = Decimal("0.02"),
    min_ask_sum: Decimal = Decimal("0.97"),
    max_ask_sum: Decimal = Decimal("1.03"),
    strong_expected_roi: Decimal = Decimal("0.04"),
    strong_lead_bps: Decimal = Decimal("2.00"),
) -> MakerMomentumEvaluation:
    if side not in {"UP", "DOWN"} or seconds_to_end < min_seconds_before_end:
        return MakerMomentumEvaluation(None, "time_or_side")
    ok, quote_reason = quotes_pass_sanity_checks(
        up_quote,
        down_quote,
        max_spread,
        min_ask_sum,
        max_ask_sum,
    )
    if not ok:
        return MakerMomentumEvaluation(None, f"quote_sanity:{quote_reason.replace(' ', '_')}")
    assert up_quote is not None and up_quote.bid is not None and up_quote.ask is not None
    assert down_quote is not None and down_quote.bid is not None and down_quote.ask is not None

    up_midpoint = (up_quote.bid + up_quote.ask) / Decimal("2")
    down_midpoint = (down_quote.bid + down_quote.ask) / Decimal("2")
    favorite = "UP" if up_midpoint > down_midpoint else "DOWN" if down_midpoint > up_midpoint else None
    if side != favorite:
        return MakerMomentumEvaluation(None, "not_favorite")

    entry = up_quote.ask if side == "UP" else down_quote.ask
    probability = probability_up if side == "UP" else Decimal("1") - probability_up
    token_id = market.token_ids[0] if side == "UP" else market.token_ids[1]
    if entry < min_entry:
        return MakerMomentumEvaluation(None, "entry_below_min")
    if entry > max_entry:
        return MakerMomentumEvaluation(None, "entry_above_max")
    if entry - trigger_price > max_chase:
        return MakerMomentumEvaluation(None, "excessive_chase")
    if probability < min_probability:
        return MakerMomentumEvaluation(None, "model_probability")
    if not recent_spot_samples_support_side(
        recent_spot_prices,
        start_price,
        side,
        spot_samples,
    ):
        return MakerMomentumEvaluation(None, "spot_alignment")

    if side == "UP":
        lead_bps = (recent_spot_prices[-1] / start_price - Decimal("1")) * Decimal("10000")
    else:
        lead_bps = (Decimal("1") - recent_spot_prices[-1] / start_price) * Decimal("10000")
    if lead_bps < min_lead_bps:
        return MakerMomentumEvaluation(None, "lead_bps")

    adjusted_probability = min(Decimal("1"), probability + flow_probability_boost)
    fee_per_stake = fee_rate * (Decimal("1") - entry)
    expected_roi = adjusted_probability / entry - Decimal("1") - fee_per_stake
    if expected_roi < min_expected_roi:
        return MakerMomentumEvaluation(None, "expected_roi")
    if expected_roi < strong_expected_roi and lead_bps < strong_lead_bps:
        return MakerMomentumEvaluation(None, "weak_flow")

    return MakerMomentumEvaluation(
        AutoTradeSignal(
            side=side,
            token_id=token_id,
            price=entry,
            reason=(
                f"maker_momentum_v2 trigger={trigger_price} entry={entry} "
                f"model_probability={probability.quantize(Decimal('0.0001'))} "
                f"flow_adjusted_probability={adjusted_probability.quantize(Decimal('0.0001'))} "
                f"expected_roi={expected_roi.quantize(Decimal('0.0001'))} "
                f"lead_bps={lead_bps.quantize(Decimal('0.01'))} "
                f"seconds_left={int(seconds_to_end)}"
            ),
        ),
        None,
    )


def choose_maker_momentum_signal(
    market: Market,
    side: str,
    trigger_price: Decimal,
    probability_up: Decimal,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    recent_spot_prices: list[Decimal],
    start_price: Decimal,
    min_seconds_before_end: Decimal = Decimal("30"),
    min_entry: Decimal = Decimal("0.60"),
    max_entry: Decimal = Decimal("0.88"),
    min_probability: Decimal = Decimal("0.55"),
    flow_probability_boost: Decimal = Decimal("0.10"),
    min_expected_roi: Decimal = Decimal("0.03"),
    min_lead_bps: Decimal = Decimal("0.50"),
    spot_samples: int = 3,
    max_chase: Decimal = Decimal("0.06"),
    fee_rate: Decimal = Decimal("0.07"),
    max_spread: Decimal = Decimal("0.02"),
    min_ask_sum: Decimal = Decimal("0.97"),
    max_ask_sum: Decimal = Decimal("1.03"),
    strong_expected_roi: Decimal = Decimal("0.04"),
    strong_lead_bps: Decimal = Decimal("2.00"),
) -> AutoTradeSignal | None:
    return evaluate_maker_momentum_signal(
        market,
        side,
        trigger_price,
        probability_up,
        up_quote,
        down_quote,
        seconds_to_end,
        recent_spot_prices,
        start_price,
        min_seconds_before_end,
        min_entry,
        max_entry,
        min_probability,
        flow_probability_boost,
        min_expected_roi,
        min_lead_bps,
        spot_samples,
        max_chase,
        fee_rate,
        max_spread,
        min_ask_sum,
        max_ask_sum,
        strong_expected_roi,
        strong_lead_bps,
    ).signal


def record_split_maker_fill(cycle: SplitMakerCycle, side: str, price: Decimal) -> Decimal:
    if side == "UP" and cycle.inventory_up > 0:
        shares = cycle.inventory_up
        cycle.inventory_up = Decimal("0")
        cycle.up_sold_price = price
    elif side == "DOWN" and cycle.inventory_down > 0:
        shares = cycle.inventory_down
        cycle.inventory_down = Decimal("0")
        cycle.down_sold_price = price
    else:
        return Decimal("0")
    proceeds = shares * price
    cycle.cash_flow += proceeds
    if cycle.inventory_up == 0 and cycle.inventory_down == 0:
        cycle.closed = True
        cycle.close_reason = "both_maker_quotes_filled"
    return proceeds


def split_maker_best_exit(
    cycle: SplitMakerCycle,
    up_book: OrderBookSnapshot,
    down_book: OrderBookSnapshot,
    fee_rate: Decimal,
) -> SplitMakerExit | None:
    if cycle.up_sold_price is not None and cycle.inventory_down > 0:
        remaining_book = down_book
        sold_book = up_book
        remaining_shares = cycle.inventory_down
    elif cycle.down_sold_price is not None and cycle.inventory_up > 0:
        remaining_book = up_book
        sold_book = down_book
        remaining_shares = cycle.inventory_up
    else:
        return None

    options: list[SplitMakerExit] = []
    direct = fill_bid_depth(remaining_book.bids, remaining_shares, fee_rate)
    if direct is not None:
        options.append(
            SplitMakerExit(
                action="sell_remaining_inventory",
                cash_delta=direct.cost - direct.fee,
                price=direct.vwap,
                fee=direct.fee,
            )
        )
    buyback = fill_ask_depth(sold_book.asks, remaining_shares, fee_rate)
    if buyback is not None:
        options.append(
            SplitMakerExit(
                action="buy_back_and_merge",
                cash_delta=remaining_shares - buyback.cost - buyback.fee,
                price=buyback.vwap,
                fee=buyback.fee,
            )
        )
    return max(options, key=lambda option: option.cash_delta, default=None)


def apply_split_maker_exit(cycle: SplitMakerCycle, exit_plan: SplitMakerExit) -> Decimal:
    cycle.cash_flow += exit_plan.cash_delta
    cycle.inventory_up = Decimal("0")
    cycle.inventory_down = Decimal("0")
    cycle.closed = True
    cycle.close_reason = exit_plan.action
    return exit_plan.cash_delta


def merge_split_maker_inventory(cycle: SplitMakerCycle) -> Decimal:
    mergeable = min(cycle.inventory_up, cycle.inventory_down)
    if mergeable <= 0:
        return Decimal("0")
    cycle.inventory_up -= mergeable
    cycle.inventory_down -= mergeable
    cycle.cash_flow += mergeable
    if cycle.inventory_up == 0 and cycle.inventory_down == 0:
        cycle.closed = True
        cycle.close_reason = "cancel_and_merge"
    return mergeable


def open_split_maker_cycle(
    bankroll: Decimal,
    slug: str,
    shares: Decimal,
) -> tuple[Decimal, SplitMakerCycle | None]:
    if shares <= 0:
        raise ValueError("Split maker shares must be positive")
    if bankroll < shares:
        logger.info("SPLIT_MAKER_SKIP insufficient bankroll=%s split_cost=%s", bankroll, shares)
        return bankroll, None
    cycle = SplitMakerCycle(
        slug=slug,
        shares=shares,
        cash_flow=-shares,
        inventory_up=shares,
        inventory_down=shares,
    )
    bankroll -= shares
    logger.info(
        "SPLIT_MAKER_SPLIT slug=%s shares=%s bankroll=%s",
        slug,
        shares,
        bankroll.quantize(Decimal("0.0001")),
    )
    return bankroll, cycle


def settle_split_maker_cycles(
    cycles: list[SplitMakerCycle],
    bankroll: Decimal,
    now_ts: int | None = None,
) -> Decimal:
    current_ts = int(time.time()) if now_ts is None else now_ts
    for cycle in cycles:
        if cycle.closed:
            continue
        match = SLUG_PATTERN.match(cycle.slug)
        if match is None or current_ts < int(match.group(2)) + 300:
            continue
        try:
            winner = fetch_winner(cycle.slug)
        except RequestException as exc:
            logger.warning("SPLIT_MAKER_SETTLE_WAIT slug=%s fetch failed: %s", cycle.slug, exc)
            continue
        if winner not in {"UP", "DOWN"}:
            continue
        payout = cycle.inventory_up if winner == "UP" else cycle.inventory_down
        bankroll += payout
        cycle.cash_flow += payout
        cycle.inventory_up = Decimal("0")
        cycle.inventory_down = Decimal("0")
        cycle.closed = True
        cycle.close_reason = "inventory_settlement"
        logger.info(
            "SPLIT_MAKER_SETTLE slug=%s winner=%s payout=%s profit=%s bankroll=%s",
            cycle.slug,
            winner,
            payout.quantize(Decimal("0.0001")),
            cycle.cash_flow.quantize(Decimal("0.0001")),
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
    price_to_beat_client = (
        PolymarketPriceToBeatClient(
            timeout=args.market_data_timeout,
            proxy_url=args.price_to_beat_proxy or args.ws_proxy,
        )
        if args.official_price_to_beat
        else None
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
    primary_side_this_window: str | None = None
    primary_cost_this_window = Decimal("0")
    primary_shares_this_window = Decimal("0")
    edge_threshold = Decimal(args.edge)
    fallback_sigma = Decimal(args.fallback_sigma)
    confirmation_jump_sigma_multiplier = Decimal(args.confirmation_jump_sigma_multiplier)
    confirmation_min_jump_usd = Decimal(args.confirmation_min_jump_usd)
    hedge_min_win_probability = Decimal(args.hedge_min_win_probability)
    hedge_fee_rate = Decimal(args.hedge_fee_rate)
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
    decision_seconds_before_end = Decimal(str(args.decision_seconds_before_end))
    min_seconds_before_end = Decimal(str(args.min_seconds_before_end))
    paper_bankroll = Decimal(args.paper_bankroll)
    paper_stake = Decimal(args.paper_stake)
    maker_shares = Decimal(args.maker_shares)
    maker_target_pair_sum = Decimal(args.maker_target_pair_sum)
    maker_fee_rate = Decimal(args.maker_fee_rate)
    maker_max_inventory_loss_rate = Decimal(args.maker_max_inventory_loss_rate)
    momentum_target_pair_sum = Decimal(args.momentum_target_pair_sum)
    momentum_min_entry = Decimal(args.momentum_min_entry)
    momentum_max_entry = Decimal(args.momentum_max_entry)
    momentum_min_probability = Decimal(args.momentum_min_probability)
    momentum_flow_probability_boost = Decimal(args.momentum_flow_probability_boost)
    momentum_min_expected_roi = Decimal(args.momentum_min_expected_roi)
    momentum_min_lead_bps = Decimal(args.momentum_min_lead_bps)
    momentum_strong_expected_roi = Decimal(args.momentum_strong_expected_roi)
    momentum_strong_lead_bps = Decimal(args.momentum_strong_lead_bps)
    momentum_max_chase = Decimal(args.momentum_max_chase)
    momentum_fee_rate = Decimal(args.momentum_fee_rate)
    momentum_max_spread = Decimal(args.momentum_max_spread)
    momentum_min_ask_sum = Decimal(args.momentum_min_ask_sum)
    momentum_max_ask_sum = Decimal(args.momentum_max_ask_sum)
    paper_positions: list[PaperPosition] = []
    split_maker_cycles: list[SplitMakerCycle] = []
    split_maker_state: SplitMakerCycle | None = None
    split_maker_groups_started = 0
    maker_momentum_state = MakerMomentumProbe()
    maker_momentum_triggers = 0
    maker_momentum_prefilter_rejects = 0
    maker_momentum_candidate_rejects = 0
    maker_momentum_signals = 0
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
        if (
            maker_shares <= 0
            or maker_target_pair_sum <= Decimal("1")
            or maker_target_pair_sum >= Decimal("2")
            or maker_fee_rate < 0
            or maker_max_inventory_loss_rate < 0
            or args.maker_start_delay_seconds < 0
            or args.maker_min_rest_seconds < 0
            or args.maker_reprice_ticks < 1
            or args.maker_unpaired_timeout_seconds < 1
            or not 0 < args.maker_force_exit_seconds < args.maker_cancel_seconds < 300
        ):
            raise ValueError("split_maker parameters are outside their allowed ranges")
        if (
            momentum_target_pair_sum <= Decimal("1")
            or momentum_target_pair_sum >= Decimal("2")
            or args.momentum_start_delay_seconds < 0
            or args.momentum_min_rest_seconds < 0
            or args.momentum_reprice_ticks < 1
            or args.momentum_confirmation_samples < 1
            or args.momentum_trigger_timeout_seconds < 1
            or not 0 < args.momentum_min_seconds_before_end < 300
            or not Decimal("0") < momentum_min_entry <= momentum_max_entry < Decimal("1")
            or not Decimal("0") <= momentum_min_probability <= Decimal("1")
            or not Decimal("0") <= momentum_flow_probability_boost <= Decimal("1")
            or momentum_min_expected_roi < 0
            or momentum_min_lead_bps < 0
            or momentum_strong_expected_roi < momentum_min_expected_roi
            or momentum_strong_lead_bps < momentum_min_lead_bps
            or args.momentum_spot_samples < 1
            or momentum_max_chase < 0
            or momentum_fee_rate < 0
            or momentum_max_spread < 0
            or momentum_min_ask_sum > momentum_max_ask_sum
        ):
            raise ValueError("maker_momentum parameters are outside their allowed ranges")
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
            paper_bankroll = settle_split_maker_cycles(split_maker_cycles, paper_bankroll)
            paper_bankroll = settle_all_paper_positions(paper_positions, paper_bankroll)
            if args.strategy == "late_favorite":
                loss_pause = args.late_pause_windows_after_loss
                loss_limit = 1 if loss_pause > 0 else 0
            elif args.strategy in {"split_maker", "maker_momentum"}:
                loss_pause = 0
                loss_limit = 0
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
                and all(cycle.closed for cycle in split_maker_cycles)
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
            primary_side_this_window = None
            primary_cost_this_window = Decimal("0")
            primary_shares_this_window = Decimal("0")
            split_maker_state = None
            maker_momentum_state = MakerMomentumProbe()
            if current_market is None:
                notifications.notify_exception(
                    "读取 Polymarket 市场",
                    RuntimeError(f"暂时无法读取市场 {slug}"),
                    key=f"market:{slug}",
                )
                time.sleep(args.interval)
                continue
            if _seconds_to_end(current_market, datetime.now(timezone.utc)) <= 0:
                logger.info("Skipping expired window: %s", current_market.slug)
                slug = next_5m_slug(current_market.slug)
                current_market = None
                continue
            elapsed_since_start = -_seconds_to_start(current_market, datetime.now(timezone.utc))
            if elapsed_since_start > Decimal(str(args.max_start_capture_delay)):
                logger.info(
                    "Skipping partial window %s: already started %ss ago",
                    current_market.slug,
                    int(elapsed_since_start),
                )
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
                time.sleep(args.interval)
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
            if start_capture_is_too_late(seconds_to_start, Decimal(str(args.max_start_capture_delay))):
                logger.info(
                    "Skipping %s: start-price capture is %ss late",
                    current_market.slug,
                    int(elapsed_since_start),
                )
                slug = next_5m_slug(current_market.slug)
                current_market = None
                time.sleep(args.interval)
                continue
            if spot.source == "CACHE":
                logger.info("Waiting for fresh start price for %s", current_market.slug)
                time.sleep(args.interval)
                continue
            if price_to_beat_client is not None:
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
                    time.sleep(args.interval)
                    continue
                boundary_timestamp_ms = int(current_market.event_start_time.timestamp() * 1000)
                try:
                    boundary_spot = price_client.polymarket_chainlink_price_near(
                        boundary_timestamp_ms,
                        args.max_boundary_sample_offset_ms,
                    )
                except Exception as exc:
                    logger.error(
                        "PRICE_ALIGNMENT_REJECTED slug=%s reason=boundary_sample error=%s",
                        current_market.slug,
                        exc,
                    )
                    slug = next_5m_slug(current_market.slug)
                    current_market = None
                    time.sleep(args.interval)
                    continue
                assert boundary_spot.observed_at_ms is not None
                boundary_offset_ms = boundary_spot.observed_at_ms - boundary_timestamp_ms
                alignment_difference = boundary_spot.price - price_to_beat.open_price
                if abs(alignment_difference) > max_price_alignment_difference:
                    logger.error(
                        "PRICE_ALIGNMENT_REJECTED slug=%s official=%s boundary_spot=%s "
                        "difference=%s max_difference=%s boundary_offset_ms=%s",
                        current_market.slug,
                        price_to_beat.open_price,
                        boundary_spot.price,
                        alignment_difference,
                        max_price_alignment_difference,
                        boundary_offset_ms,
                    )
                    slug = next_5m_slug(current_market.slug)
                    current_market = None
                    time.sleep(args.interval)
                    continue
                start_price = price_to_beat.open_price
                alignment_record = {
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "slug": current_market.slug,
                    "status": "VERIFIED",
                    "official_price_to_beat": str(start_price),
                    "boundary_chainlink_price": str(boundary_spot.price),
                    "boundary_chainlink_timestamp_ms": boundary_spot.observed_at_ms,
                    "boundary_offset_ms": boundary_offset_ms,
                    "alignment_difference": str(alignment_difference),
                    "capture_delay_seconds": str(elapsed_since_start),
                    "endpoint_timestamp_ms": price_to_beat.timestamp_ms,
                    "endpoint_incomplete": price_to_beat.incomplete,
                }
                logger.info(
                    "PRICE_ALIGNMENT VERIFIED slug=%s official=%s boundary_spot=%s "
                    "difference=%s boundary_offset_ms=%s capture_delay=%ss",
                    current_market.slug,
                    start_price,
                    boundary_spot.price,
                    alignment_difference,
                    boundary_offset_ms,
                    elapsed_since_start,
                )
                if args.price_alignment_jsonl:
                    alignment_path = Path(args.price_alignment_jsonl)
                    alignment_path.parent.mkdir(parents=True, exist_ok=True)
                    with alignment_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(alignment_record, ensure_ascii=True) + "\n")
            else:
                start_price = spot.price
            prices = [spot.price]
            logger.info("Captured start_price=%s for %s", start_price, current_market.slug)
        else:
            prices.append(spot.price)

        sigma = estimate_sigma_per_sqrt_second(prices, Decimal(args.interval), fallback_sigma)
        fair = btc_up_probability(start_price, spot.price, max(Decimal("0"), seconds_to_end), sigma)
        up_book: OrderBookSnapshot | None = None
        down_book: OrderBookSnapshot | None = None
        if args.strategy in {"split_maker", "maker_momentum"}:
            up_book, down_book = outcome_books(clob, current_market)
            up_quote = up_book.quote if up_book is not None else None
            down_quote = down_book.quote if down_book is not None else None
        else:
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
            and args.strategy == "split_maker"
            and not risk_pause_active_for_window
            and not notifications.trading_paused
            and args.paper_trading
            and up_book is not None
            and down_book is not None
        ):
            now_monotonic = time.monotonic()
            elapsed_seconds = Decimal("300") - seconds_to_end
            if (
                split_maker_state is None
                and signals_this_window == 0
                and elapsed_seconds >= Decimal(str(args.maker_start_delay_seconds))
                and seconds_to_end > Decimal(str(args.maker_cancel_seconds))
            ):
                paper_bankroll, split_maker_state = open_split_maker_cycle(
                    paper_bankroll,
                    current_market.slug,
                    maker_shares,
                )
                if split_maker_state is not None:
                    split_maker_cycles.append(split_maker_state)
                    split_maker_groups_started += 1
                    signals_this_window += 1

            state = split_maker_state
            if state is not None and not state.closed:
                tick_size = Decimal(current_market.minimum_tick_size)
                no_side_filled = state.up_sold_price is None and state.down_sold_price is None
                if seconds_to_end <= Decimal(str(args.maker_cancel_seconds)) and no_side_filled:
                    merged = merge_split_maker_inventory(state)
                    paper_bankroll += merged
                    logger.info(
                        "SPLIT_MAKER_CANCEL_MERGE slug=%s merged=%s profit=%s bankroll=%s",
                        state.slug,
                        merged,
                        state.cash_flow.quantize(Decimal("0.0001")),
                        paper_bankroll.quantize(Decimal("0.0001")),
                    )
                else:
                    plan = plan_split_maker_quotes(
                        fair.probability_up,
                        up_book,
                        down_book,
                        maker_target_pair_sum,
                        tick_size,
                    )
                    if plan is not None:
                        desired_up = plan.up_price
                        desired_down = plan.down_price
                        if state.down_sold_price is not None:
                            desired_up = max(
                                desired_up,
                                round_up_to_tick(
                                    maker_target_pair_sum - state.down_sold_price,
                                    tick_size,
                                ),
                            )
                        if state.up_sold_price is not None:
                            desired_down = max(
                                desired_down,
                                round_up_to_tick(
                                    maker_target_pair_sum - state.up_sold_price,
                                    tick_size,
                                ),
                            )
                        reprice_distance = tick_size * Decimal(args.maker_reprice_ticks)
                        if state.inventory_up > 0 and (
                            state.up_quote is None or abs(desired_up - state.up_quote) >= reprice_distance
                        ):
                            state.up_quote = desired_up
                            state.up_quote_started_at = now_monotonic
                            logger.info("SPLIT_MAKER_QUOTE slug=%s side=UP price=%s", state.slug, desired_up)
                        if state.inventory_down > 0 and (
                            state.down_quote is None or abs(desired_down - state.down_quote) >= reprice_distance
                        ):
                            state.down_quote = desired_down
                            state.down_quote_started_at = now_monotonic
                            logger.info("SPLIT_MAKER_QUOTE slug=%s side=DOWN price=%s", state.slug, desired_down)

                    if state.inventory_up > 0 and maker_quote_crossed(
                        up_book,
                        state.up_quote,
                        state.up_quote_started_at,
                        now_monotonic,
                        args.maker_min_rest_seconds,
                    ):
                        proceeds = record_split_maker_fill(state, "UP", state.up_quote)
                        paper_bankroll += proceeds
                        logger.info(
                            "SPLIT_MAKER_FILL slug=%s side=UP price=%s proceeds=%s bankroll=%s",
                            state.slug,
                            state.up_quote,
                            proceeds.quantize(Decimal("0.0001")),
                            paper_bankroll.quantize(Decimal("0.0001")),
                        )
                    if state.inventory_down > 0 and maker_quote_crossed(
                        down_book,
                        state.down_quote,
                        state.down_quote_started_at,
                        now_monotonic,
                        args.maker_min_rest_seconds,
                    ):
                        proceeds = record_split_maker_fill(state, "DOWN", state.down_quote)
                        paper_bankroll += proceeds
                        logger.info(
                            "SPLIT_MAKER_FILL slug=%s side=DOWN price=%s proceeds=%s bankroll=%s",
                            state.slug,
                            state.down_quote,
                            proceeds.quantize(Decimal("0.0001")),
                            paper_bankroll.quantize(Decimal("0.0001")),
                        )

                    if state.closed:
                        logger.info(
                            "SPLIT_MAKER_LOCKED slug=%s profit=%s bankroll=%s",
                            state.slug,
                            state.cash_flow.quantize(Decimal("0.0001")),
                            paper_bankroll.quantize(Decimal("0.0001")),
                        )
                    else:
                        one_side_filled = (state.up_sold_price is None) != (state.down_sold_price is None)
                        if one_side_filled:
                            if state.unpaired_since is None:
                                state.unpaired_since = now_monotonic
                            exit_plan = split_maker_best_exit(
                                state,
                                up_book,
                                down_book,
                                maker_fee_rate,
                            )
                            if exit_plan is not None:
                                projected_profit = state.cash_flow + exit_plan.cash_delta
                                projected_loss_rate = max(
                                    Decimal("0"),
                                    -projected_profit / state.shares,
                                )
                                timed_out = (
                                    now_monotonic - state.unpaired_since
                                    >= args.maker_unpaired_timeout_seconds
                                )
                                force_exit = seconds_to_end <= Decimal(str(args.maker_force_exit_seconds))
                                loss_limit_hit = projected_loss_rate >= maker_max_inventory_loss_rate
                                if timed_out or force_exit or loss_limit_hit:
                                    cash_delta = apply_split_maker_exit(state, exit_plan)
                                    paper_bankroll += cash_delta
                                    logger.info(
                                        "SPLIT_MAKER_EXIT slug=%s action=%s price=%s fee=%s "
                                        "profit=%s bankroll=%s trigger=%s",
                                        state.slug,
                                        exit_plan.action,
                                        exit_plan.price.quantize(Decimal("0.0001")),
                                        exit_plan.fee.quantize(Decimal("0.0001")),
                                        state.cash_flow.quantize(Decimal("0.0001")),
                                        paper_bankroll.quantize(Decimal("0.0001")),
                                        (
                                            "loss_limit"
                                            if loss_limit_hit
                                            else "timeout"
                                            if timed_out
                                            else "time_cutoff"
                                        ),
                                    )

        if (
            args.auto_trade
            and args.strategy == "maker_momentum"
            and signals_this_window == 0
            and not risk_pause_active_for_window
            and not notifications.trading_paused
            and args.paper_trading
            and up_book is not None
            and down_book is not None
        ):
            now_monotonic = time.monotonic()
            elapsed_seconds = Decimal("300") - seconds_to_end
            state = maker_momentum_state
            if (
                elapsed_seconds >= Decimal(str(args.momentum_start_delay_seconds))
                and seconds_to_end >= Decimal(str(args.momentum_min_seconds_before_end))
            ):
                tick_size = Decimal(current_market.minimum_tick_size)
                if state.candidate_side is None:
                    plan = plan_split_maker_quotes(
                        fair.probability_up,
                        up_book,
                        down_book,
                        momentum_target_pair_sum,
                        tick_size,
                    )
                    if plan is not None:
                        reprice_distance = tick_size * Decimal(args.momentum_reprice_ticks)
                        if state.up_quote is None or abs(plan.up_price - state.up_quote) >= reprice_distance:
                            state.up_quote = plan.up_price
                            state.up_quote_started_at = now_monotonic
                        if state.down_quote is None or abs(plan.down_price - state.down_quote) >= reprice_distance:
                            state.down_quote = plan.down_price
                            state.down_quote_started_at = now_monotonic

                    up_crossed = maker_quote_crossed(
                        up_book,
                        state.up_quote,
                        state.up_quote_started_at,
                        now_monotonic,
                        args.momentum_min_rest_seconds,
                    )
                    down_crossed = maker_quote_crossed(
                        down_book,
                        state.down_quote,
                        state.down_quote_started_at,
                        now_monotonic,
                        args.momentum_min_rest_seconds,
                    )
                    if up_crossed != down_crossed:
                        trigger_side = "UP" if up_crossed else "DOWN"
                        trigger_price = state.up_quote if up_crossed else state.down_quote
                        assert trigger_price is not None
                        maker_momentum_triggers += 1
                        prefilter = evaluate_maker_momentum_signal(
                            market=current_market,
                            side=trigger_side,
                            trigger_price=trigger_price,
                            probability_up=fair.probability_up,
                            up_quote=up_quote,
                            down_quote=down_quote,
                            seconds_to_end=seconds_to_end,
                            recent_spot_prices=prices,
                            start_price=start_price,
                            min_seconds_before_end=Decimal(
                                str(args.momentum_min_seconds_before_end)
                            ),
                            min_entry=momentum_min_entry,
                            max_entry=momentum_max_entry,
                            min_probability=momentum_min_probability,
                            flow_probability_boost=momentum_flow_probability_boost,
                            min_expected_roi=momentum_min_expected_roi,
                            min_lead_bps=momentum_min_lead_bps,
                            spot_samples=args.momentum_spot_samples,
                            max_chase=momentum_max_chase,
                            fee_rate=momentum_fee_rate,
                            max_spread=momentum_max_spread,
                            min_ask_sum=momentum_min_ask_sum,
                            max_ask_sum=momentum_max_ask_sum,
                            strong_expected_roi=momentum_strong_expected_roi,
                            strong_lead_bps=momentum_strong_lead_bps,
                        )
                        if prefilter.signal is None:
                            maker_momentum_prefilter_rejects += 1
                            logger.info(
                                "MAKER_MOMENTUM_PREFILTER_REJECT slug=%s side=%s "
                                "trigger=%s reason=%s",
                                current_market.slug,
                                trigger_side,
                                trigger_price,
                                prefilter.rejection_reason,
                            )
                            maker_momentum_state = MakerMomentumProbe()
                            state = maker_momentum_state
                        else:
                            state.candidate_side = trigger_side
                            state.trigger_price = trigger_price
                            state.candidate_started_at = now_monotonic
                            state.confirmations = 0
                            logger.info(
                                "MAKER_MOMENTUM_TRIGGER slug=%s side=%s trigger=%s "
                                "seconds_left=%s prefilter=passed",
                                current_market.slug,
                                state.candidate_side,
                                state.trigger_price,
                                int(seconds_to_end),
                            )

                if (
                    state.candidate_side is not None
                    and state.trigger_price is not None
                    and state.candidate_started_at is not None
                ):
                    candidate_book = up_book if state.candidate_side == "UP" else down_book
                    candidate_bid = candidate_book.quote.bid
                    if candidate_bid is not None and candidate_bid >= state.trigger_price:
                        state.confirmations += 1
                    else:
                        state.confirmations = 0

                    evaluation = None
                    if state.confirmations >= args.momentum_confirmation_samples:
                        evaluation = evaluate_maker_momentum_signal(
                            market=current_market,
                            side=state.candidate_side,
                            trigger_price=state.trigger_price,
                            probability_up=fair.probability_up,
                            up_quote=up_quote,
                            down_quote=down_quote,
                            seconds_to_end=seconds_to_end,
                            recent_spot_prices=prices,
                            start_price=start_price,
                            min_seconds_before_end=Decimal(
                                str(args.momentum_min_seconds_before_end)
                            ),
                            min_entry=momentum_min_entry,
                            max_entry=momentum_max_entry,
                            min_probability=momentum_min_probability,
                            flow_probability_boost=momentum_flow_probability_boost,
                            min_expected_roi=momentum_min_expected_roi,
                            min_lead_bps=momentum_min_lead_bps,
                            spot_samples=args.momentum_spot_samples,
                            max_chase=momentum_max_chase,
                            fee_rate=momentum_fee_rate,
                            max_spread=momentum_max_spread,
                            min_ask_sum=momentum_min_ask_sum,
                            max_ask_sum=momentum_max_ask_sum,
                            strong_expected_roi=momentum_strong_expected_roi,
                            strong_lead_bps=momentum_strong_lead_bps,
                        )
                    signal = evaluation.signal if evaluation is not None else None
                    if signal is not None:
                        signals_this_window = window_trade_count_after_attempt(
                            signals_this_window,
                            live=False,
                        )
                        maker_momentum_signals += 1
                        logger.info(
                            "MAKER_MOMENTUM_SIGNAL slug=%s confirmations=%s reason=%s",
                            current_market.slug,
                            state.confirmations,
                            signal.reason,
                        )
                        paper_bankroll = open_paper_position(
                            paper_positions,
                            paper_bankroll,
                            current_market.slug,
                            signal,
                            paper_stake,
                            momentum_fee_rate,
                        )
                    elif evaluation is not None:
                        maker_momentum_candidate_rejects += 1
                        logger.info(
                            "MAKER_MOMENTUM_REJECT slug=%s side=%s confirmations=%s reason=%s",
                            current_market.slug,
                            state.candidate_side,
                            state.confirmations,
                            evaluation.rejection_reason,
                        )
                        maker_momentum_state = MakerMomentumProbe()
                    elif (
                        now_monotonic - state.candidate_started_at
                        >= args.momentum_trigger_timeout_seconds
                    ):
                        maker_momentum_candidate_rejects += 1
                        logger.info(
                            "MAKER_MOMENTUM_REJECT slug=%s side=%s confirmations=%s "
                            "reason=confirmation_timeout",
                            current_market.slug,
                            state.candidate_side,
                            state.confirmations,
                        )
                        maker_momentum_state = MakerMomentumProbe()

        if (
            args.auto_trade
            and args.strategy not in {"split_maker", "maker_momentum"}
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
                    protective_signal = choose_protective_hedge_signal(
                        current_market,
                        primary_side_this_window,
                        fair.probability_up,
                        up_quote,
                        down_quote,
                        seconds_to_end,
                        decision_seconds_before_end,
                        min_seconds_before_end,
                        max_entry,
                        edge_threshold,
                        hedge_min_win_probability,
                        max_spread,
                        min_ask_sum,
                        max_ask_sum,
                    )
                    signal = protective_signal
            if signal is not None:
                if notifications.trading_paused:
                    logger.warning("AUTO_SIGNAL blocked because Telegram trading pause is active.")
                    candidate_side = None
                    candidate_confirmations = 0
                    time.sleep(args.interval)
                    continue
                if spot_age > Decimal(str(args.max_spot_age)):
                    logger.info("SIGNAL_REJECTED stale_spot_age=%.1fs", float(spot_age))
                    candidate_side = None
                    candidate_confirmations = 0
                    time.sleep(args.interval)
                    continue
                is_protective_hedge = signal.reason.startswith("protective_hedge")
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
                    time.sleep(args.interval)
                    continue
                jump_reset, adverse_jump, jump_threshold = adverse_jump_exceeds_dynamic_threshold(
                    prices,
                    signal.side,
                    fair.sigma_per_sqrt_second,
                    Decimal(str(args.interval)),
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
                    args.hedge_signal_confirmations
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
                    time.sleep(args.interval)
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
                        time.sleep(args.interval)
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
                        paper_bankroll = open_paper_position(
                            paper_positions,
                            paper_bankroll,
                            current_market.slug,
                            signal,
                            paper_stake,
                            late_fee_rate if args.strategy == "late_favorite" else Decimal("0"),
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
                    if not live_session_should_continue(live_orders_submitted, args.max_live_orders):
                        logger.warning("LIVE ORDER LIMIT reached=%s. Exiting.", live_orders_submitted)
                        return
                    if notional > max_live_notional:
                        logger.warning(
                            "LIVE SIGNAL SKIPPED notional=%s exceeds hard cap=%s; continuing.",
                            notional,
                            max_live_notional,
                        )
                        time.sleep(args.interval)
                        continue
                    if notifications.trading_paused:
                        logger.warning("LIVE ORDER blocked by Telegram trading pause before submission.")
                        time.sleep(args.interval)
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
                        time.sleep(args.interval)
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
                        time.sleep(args.interval)
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

        time.sleep(args.interval)

    if args.paper_trading:
        paper_bankroll = settle_split_maker_cycles(split_maker_cycles, paper_bankroll)
        paper_bankroll = settle_all_paper_positions(paper_positions, paper_bankroll)
        open_positions = sum(1 for position in paper_positions if not position.settled)
        if args.strategy == "split_maker":
            closed_cycles = [cycle for cycle in split_maker_cycles if cycle.closed]
            logger.info(
                "SPLIT_MAKER_SUMMARY started=%s completed=%s open=%s realized_profit=%s",
                split_maker_groups_started,
                len(closed_cycles),
                len(split_maker_cycles) - len(closed_cycles),
                sum((cycle.cash_flow for cycle in closed_cycles), Decimal("0")).quantize(
                    Decimal("0.0001")
                ),
            )
        if args.strategy == "maker_momentum":
            logger.info(
                "MAKER_MOMENTUM_SUMMARY triggers=%s prefilter_rejects=%s "
                "candidate_rejects=%s signals=%s",
                maker_momentum_triggers,
                maker_momentum_prefilter_rejects,
                maker_momentum_candidate_rejects,
                maker_momentum_signals,
            )
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
