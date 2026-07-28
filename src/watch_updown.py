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
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
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
    OrderBookSnapshot,
    OrderBookQuote,
    OrderQuoteExpiredError,
)
from src.price_alignment import PolymarketPriceToBeatClient, StableOpenPriceTracker
from src.price_signal import SpotPriceClient
from src.polygon_split import splitter_from_config
from src.reversal_runtime import (
    ReversalRuntime,
    market_health_from_books,
    reversal_startup_self_check,
)
from src.reversal_v11 import Direction, ReversalV11
from src.telegram_commands import DEFAULT_LAUNCH_STRATEGY, LIVE_STRATEGIES, STRATEGY_LABELS
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


@dataclass(frozen=True)
class SmartScoreBreakdown:
    total: Decimal
    required: Decimal
    edge: Decimal
    trend: Decimal
    market: Decimal
    stability: Decimal
    timing: Decimal


@dataclass
class SignalConfirmationState:
    side: str | None = None
    confirmations: int = 0
    started_at: float | None = None
    initial_price: Decimal | None = None

    def reset(self) -> None:
        self.side = None
        self.confirmations = 0
        self.started_at = None
        self.initial_price = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch rolling BTC 5m Up/Down Polymarket windows.")
    parser.add_argument("--slug", required=True, help="Current BTC 5m event slug or Polymarket event URL.")
    parser.add_argument("--duration", type=int, default=0, help="Total watch duration in seconds; 0 means unlimited.")
    parser.add_argument("--interval", type=int, default=10, help="Polling interval in seconds.")
    parser.add_argument("--price-source", default="POLYMARKET_CHAINLINK", help="POLYMARKET_CHAINLINK (strict default), AUTO (Chainlink + free exchanges), CHAINLINK, BINANCE, COINBASE, KRAKEN, or COINGECKO.")
    parser.add_argument("--edge", default="0.02", help="Minimum theoretical edge for BUY_UP/BUY_DOWN.")
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
        default=DEFAULT_LAUNCH_STRATEGY,
        choices=list(STRATEGY_LABELS),
    )
    parser.add_argument("--decision-seconds-before-end", type=int, default=120)
    parser.add_argument("--min-seconds-before-end", type=int, default=25)
    parser.add_argument("--signal-confirmations", type=int, default=2)
    parser.add_argument("--trend-confirmation-samples", type=int, default=3)
    parser.add_argument(
        "--one-way-entry-seconds",
        type=int,
        default=100,
        help="Start evaluating the one-way primary entry 100 seconds before settlement.",
    )
    parser.add_argument(
        "--one-way-entry-cutoff-seconds",
        type=int,
        default=25,
        help="Stop opening the primary one-way position 25 seconds before settlement.",
    )
    parser.add_argument("--one-way-min-entry", default="0.60")
    parser.add_argument("--one-way-max-entry", default="0.70")
    parser.add_argument(
        "--one-way-trend-samples",
        type=int,
        default=5,
        help="Recent pre-entry Chainlink samples that must remain one-sided and never pull back.",
    )
    parser.add_argument(
        "--one-way-reversal-seconds",
        type=float,
        default=5.0,
        help="Seconds BTC must continuously remain beyond the buffered open during the final reversal window.",
    )
    parser.add_argument(
        "--one-way-reversal-early-seconds",
        type=float,
        default=10.0,
        help="Required reversal persistence while more than the final reversal window remains.",
    )
    parser.add_argument(
        "--one-way-reversal-final-window-seconds",
        type=int,
        default=30,
        help="Use --one-way-reversal-seconds at or inside this many seconds before settlement.",
    )
    parser.add_argument("--one-way-reversal-min-usd", default="3.00")
    parser.add_argument("--one-way-reversal-min-bid", default="0.55")
    parser.add_argument("--one-way-reversal-max-entry", default="0.80")
    parser.add_argument(
        "--one-way-reversal-min-loss-reduction-percent",
        default="0.10",
        help="Minimum maximum-loss reduction as a fraction of aggregate primary cost.",
    )
    parser.add_argument(
        "--one-way-reversal-min-loss-reduction-notional",
        default="0.25",
        help="Absolute minimum maximum-loss reduction in paper/live notional units.",
    )
    parser.add_argument(
        "--trend-pullback-tolerance-usd",
        default="1.00",
        help="Normal-entry BTC pullback tolerance across trend samples; low-entry signals remain strict.",
    )
    parser.add_argument(
        "--trend-pullback-tolerance-percent",
        default="25",
        help="Normal-entry pullback tolerance as a percent of the largest sampled lead; the larger USD/percent value is used.",
    )
    parser.add_argument("--confirmation-jump-sigma-multiplier", default="1.25")
    parser.add_argument("--confirmation-min-jump-usd", default="3.00")
    parser.add_argument("--hedge-signal-confirmations", type=int, default=2)
    parser.add_argument("--hedge-confirmation-min-seconds", type=float, default=2.0)
    parser.add_argument("--hedge-max-price-worsening", default="0.05")
    parser.add_argument("--hedge-min-win-probability", default="0.53")
    parser.add_argument("--hedge-min-edge", default="0.01")
    parser.add_argument("--hedge-fee-rate", default="0.07")
    parser.add_argument("--hedge-entry-start-seconds", type=int, default=300)
    parser.add_argument("--hedge-entry-cutoff-seconds", type=int, default=1)
    parser.add_argument("--hedge-open-cross-min-usd", default="1.00")
    parser.add_argument("--hedge-open-cross-sigma-multiplier", default="1.00")
    parser.add_argument("--hedge-market-reversal-threshold", default="0.55")
    parser.add_argument("--hedge-max-entry", default="0.99")
    parser.add_argument("--hedge-max-spread", default="0.10")
    parser.add_argument(
        "--hedge-max-live-notional",
        default="0",
        help=(
            "Optional absolute cap for aggregate protection in pUSD; "
            "0 makes the cap track aggregate primary fill cost 1:1."
        ),
    )
    parser.add_argument("--final-poll-seconds", type=int, default=30)
    parser.add_argument("--final-poll-interval", type=float, default=1.0)
    parser.add_argument("--post-fill-poll-interval", type=float, default=1.0)
    parser.add_argument("--pre-submit-max-adverse-ask-drop", default="0.02")
    parser.add_argument("--pre-submit-max-ask-worsening", default="0.02")
    parser.add_argument("--pre-submit-max-quote-age-seconds", type=float, default=1.0)
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
    parser.add_argument(
        "--official-open-confirmations",
        type=int,
        default=2,
        help="Matching Price to Beat reads required before accepting the official threshold.",
    )
    parser.add_argument(
        "--official-open-stable-seconds",
        type=float,
        default=5.0,
        help="Minimum time an unchanged Price to Beat must remain stable before use.",
    )
    parser.add_argument("--min-win-probability", default="0.55")
    parser.add_argument(
        "--probability-shrinkage",
        default="1.00",
        help="Shrink fair-value probability toward 0.50 before evaluating edge; 1 disables calibration.",
    )
    parser.add_argument("--smart-score-threshold", default="70")
    parser.add_argument("--smart-score-entry-seconds", type=int, default=100)
    parser.add_argument("--smart-score-cutoff-seconds", type=int, default=25)
    parser.add_argument("--smart-score-min-probability", default="0.52")
    parser.add_argument("--smart-score-fee-rate", default="0.07")
    parser.add_argument("--smart-score-slippage", default="0.01")
    parser.add_argument("--smart-score-trend-samples", type=int, default=3)
    parser.add_argument("--smart-score-stability-samples", type=int, default=3)
    parser.add_argument("--open-060-entry-seconds", type=int, default=300)
    parser.add_argument("--open-060-cutoff-seconds", type=int, default=270)
    parser.add_argument("--open-060-target", default="0.60")
    parser.add_argument("--open-060-slippage", default="0.01")
    parser.add_argument("--open-060-fee-rate", default="0.07")
    parser.add_argument("--open-060-initial-ask", default="0.50")
    parser.add_argument("--low-entry-cutoff", default="0.55")
    parser.add_argument("--low-entry-min-win-probability", default="0.61")
    parser.add_argument("--low-entry-confirmation-samples", type=int, default=3)
    parser.add_argument("--min-entry", default="0.50")
    parser.add_argument("--max-entry", default="0.78")
    parser.add_argument("--max-spread", default="0.05", help="Max bid/ask spread allowed for a primary entry.")
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
    parser.add_argument("--max-live-notional", default="4.05", help="Hard principal cap per live order in pUSD.")
    parser.add_argument(
        "--late-max-live-notional",
        default="4.70",
        help="Legacy cap retained for backward-compatible command parsing.",
    )
    parser.add_argument("--live-order-type", choices=["FAK", "FOK"], default="FAK")
    parser.add_argument(
        "--live-buy-slippage",
        default="0.03",
        help="Maximum price improvement above the observed ask for live FAK/FOK buys.",
    )
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
    parser.add_argument(
        "--paper-shares",
        default="0",
        help="Simulated shares per signal; when positive, overrides --paper-stake.",
    )
    parser.add_argument(
        "--reversal-state-json",
        default="data/reversal_v11_state.json",
        help="Crash-safe persistent state for reversal_v11.",
    )
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
    parser.add_argument(
        "--disable-telegram-commands",
        action="store_true",
        help="Disable Telegram command polling while retaining outbound notifications.",
    )
    parser.add_argument(
        "--disable-discord",
        action="store_true",
        help="Disable Discord notifications for this process.",
    )
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


def executable_ask_depth(
    book: OrderBookSnapshot,
    maximum_price: Decimal,
) -> Decimal:
    if maximum_price <= 0:
        return Decimal("0")
    return sum(
        (
            level.size
            for level in book.asks
            if level.price <= maximum_price and level.size > 0
        ),
        Decimal("0"),
    )


def effective_pullback_tolerance(
    fixed_usd: Decimal,
    reference_move_usd: Decimal,
    percent: Decimal,
) -> Decimal:
    if fixed_usd < 0 or reference_move_usd < 0 or percent < 0:
        raise ValueError("Pullback tolerance inputs must be valid and non-negative")
    return max(fixed_usd, reference_move_usd * percent / Decimal("100"))


def recent_spot_samples_support_side(
    prices: list[Decimal],
    start_price: Decimal,
    side: str,
    sample_count: int,
    pullback_tolerance_usd: Decimal = Decimal("0"),
    pullback_tolerance_percent: Decimal = Decimal("0"),
) -> bool:
    if (
        sample_count < 1
        or len(prices) < sample_count
        or start_price <= 0
        or pullback_tolerance_usd < 0
        or pullback_tolerance_percent < 0
    ):
        return False
    recent = prices[-sample_count:]
    if side == "UP":
        distances = [price - start_price for price in recent]
        peak_distance = max(distances)
        tolerance = effective_pullback_tolerance(
            pullback_tolerance_usd,
            peak_distance,
            pullback_tolerance_percent,
        )
        return all(distance > 0 for distance in distances) and peak_distance - distances[-1] <= tolerance
    if side == "DOWN":
        distances = [start_price - price for price in recent]
        peak_distance = max(distances)
        tolerance = effective_pullback_tolerance(
            pullback_tolerance_usd,
            peak_distance,
            pullback_tolerance_percent,
        )
        return all(distance > 0 for distance in distances) and peak_distance - distances[-1] <= tolerance
    return False


def protective_open_cross_buffer(
    start_price: Decimal,
    sigma_per_sqrt_second: Decimal,
    confirmation_seconds: Decimal,
    minimum_buffer_usd: Decimal,
    sigma_multiplier: Decimal,
) -> Decimal:
    if (
        start_price <= 0
        or sigma_per_sqrt_second < 0
        or confirmation_seconds < 0
        or minimum_buffer_usd < 0
        or sigma_multiplier < 0
    ):
        raise ValueError("Protective open-cross inputs must be non-negative and start price positive")
    volatility_buffer = (
        start_price
        * sigma_per_sqrt_second
        * Decimal(str(math.sqrt(float(confirmation_seconds))))
        * sigma_multiplier
    )
    return max(minimum_buffer_usd, volatility_buffer)


def protective_spot_confirms_open_cross(
    prices: list[Decimal],
    start_price: Decimal,
    side: str,
    buffer_usd: Decimal,
) -> bool:
    if not prices or buffer_usd < 0:
        return False
    current = prices[-1]
    previous = prices[-2] if len(prices) >= 2 else None
    if side == "UP":
        threshold = start_price + buffer_usd
        if current < threshold:
            return False
        return previous is None or previous < threshold or current >= previous
    if side == "DOWN":
        threshold = start_price - buffer_usd
        if current > threshold:
            return False
        return previous is None or previous > threshold or current <= previous
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
    if strategy == "late_one_way":
        return min(2, configured_limit)
    if strategy in {"smart_score", "open_060", "open_060_late_070"}:
        return min(1, configured_limit)
    if is_fair_value_strategy(strategy):
        # --max-trades remains the primary-entry allowance. Aggregate protection
        # gets one additional reserved matched-order slot.
        return configured_limit + 1
    return configured_limit


def is_fair_value_strategy(strategy: str) -> bool:
    return strategy in {"fair_value_edge", "late_070", "open_060_late_070"}


def primary_signal_confirmation_count(strategy: str, configured_count: int) -> int:
    """Return the confirmation count for non-protective primary signals."""
    if strategy in {"late_070", "open_060_late_070"}:
        return 1
    return configured_count


def choose_open_060_signal(
    market: Market,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    previous_up_ask: Decimal,
    previous_down_ask: Decimal,
    entry_seconds: Decimal = Decimal("300"),
    cutoff_seconds: Decimal = Decimal("270"),
    target: Decimal = Decimal("0.60"),
    slippage: Decimal = Decimal("0.01"),
    max_spread: Decimal = Decimal("0.05"),
    min_ask_sum: Decimal = Decimal("0.90"),
    max_ask_sum: Decimal = Decimal("1.10"),
) -> AutoTradeSignal | None:
    if (
        seconds_to_end > entry_seconds
        or seconds_to_end < cutoff_seconds
        or target <= 0
        or target >= 1
        or slippage < 0
    ):
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

    crossed: list[tuple[str, str, Decimal, Decimal]] = []
    if previous_up_ask < target <= up_quote.ask:
        crossed.append(("UP", market.token_ids[0], up_quote.ask, up_quote.bid or Decimal("0")))
    if previous_down_ask < target <= down_quote.ask:
        crossed.append(("DOWN", market.token_ids[1], down_quote.ask, down_quote.bid or Decimal("0")))
    if not crossed:
        return None

    # A sane ask sum prevents both asks from being at or above 0.60, but keep
    # deterministic tie-breaking for synthetic tests and unusual books.
    side, token_id, observed_ask, bid = max(
        crossed,
        key=lambda item: (item[2], item[3], item[0] == "UP"),
    )
    # Preserve the configured 0.01 allowance at the target, but when polling
    # first observes the ask above that planned price, simulate the fill at the
    # actual observed ask instead of inventing a cheaper 0.61 fill.
    entry = min(max(target + slippage, observed_ask), Decimal("0.99"))
    return AutoTradeSignal(
        side=side,
        token_id=token_id,
        price=entry,
        reason=(
            f"open_060 first_cross target={target} observed_ask={observed_ask} "
            f"previous_ask={previous_up_ask if side == 'UP' else previous_down_ask} "
            f"slippage={slippage} seconds_left={int(seconds_to_end)}"
        ),
    )


def refresh_open_060_signal(
    market: Market,
    side: str,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    cutoff_seconds: Decimal = Decimal("270"),
    target: Decimal = Decimal("0.60"),
    slippage: Decimal = Decimal("0.01"),
    max_spread: Decimal = Decimal("0.05"),
    min_ask_sum: Decimal = Decimal("0.90"),
    max_ask_sum: Decimal = Decimal("1.10"),
) -> AutoTradeSignal | None:
    if side not in {"UP", "DOWN"} or seconds_to_end < cutoff_seconds:
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
    quote = up_quote if side == "UP" else down_quote
    if quote.ask < target:
        return None
    entry = min(max(target + slippage, quote.ask), Decimal("0.99"))
    return AutoTradeSignal(
        side=side,
        token_id=market.token_ids[0] if side == "UP" else market.token_ids[1],
        price=entry,
        reason=(
            f"open_060 first_cross target={target} observed_ask={quote.ask} "
            f"slippage={slippage} seconds_left={int(seconds_to_end)} "
            "pre_submit_refresh=true"
        ),
    )


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


def buy_limit_price_with_slippage(
    ask_price: Decimal,
    slippage: Decimal,
    tick_size: str,
    maximum_price: Decimal,
) -> Decimal:
    tick = Decimal(tick_size)
    if ask_price <= 0 or tick <= 0 or slippage < 0:
        raise ValueError("Buy price, tick size, and slippage must be valid")
    capped = min(ask_price + slippage, maximum_price, Decimal("1") - tick)
    rounded = (capped / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    return max(ask_price, rounded)


def required_fair_value_edge(
    entry_price: Decimal,
    seconds_to_end: Decimal,
    base_edge: Decimal,
) -> Decimal:
    required = base_edge
    if seconds_to_end < Decimal("45"):
        required += Decimal("0.02")
    if entry_price > Decimal("0.65"):
        required += (entry_price - Decimal("0.65")) * Decimal("0.25")
    return required


def buy_limit_price_preserving_edge(
    ask_price: Decimal,
    slippage: Decimal,
    tick_size: str,
    maximum_price: Decimal,
    probability: Decimal,
    seconds_to_end: Decimal,
    base_edge: Decimal,
) -> Decimal:
    tick = Decimal(tick_size)
    candidate = buy_limit_price_with_slippage(
        ask_price,
        slippage,
        tick_size,
        maximum_price,
    )
    while candidate > ask_price:
        if probability - candidate >= required_fair_value_edge(
            candidate,
            seconds_to_end,
            base_edge,
        ):
            return candidate
        candidate -= tick
    return ask_price


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
    low_entry_min_win_probability: Decimal = Decimal("0.61"),
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
    required_edge = required_fair_value_edge(entry, seconds_to_end, edge_threshold)
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


def _clamp_unit(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("1"), value))


def _smart_trend_quality(
    recent_spot_prices: list[Decimal],
    start_price: Decimal,
    side: str,
    sample_count: int,
) -> Decimal:
    if sample_count < 1 or len(recent_spot_prices) < sample_count or start_price <= 0:
        return Decimal("0")
    recent = recent_spot_prices[-sample_count:]
    distances = [
        price - start_price if side == "UP" else start_price - price
        for price in recent
    ]
    correct_fraction = Decimal(sum(distance > 0 for distance in distances)) / Decimal(
        sample_count
    )
    if all(distance > 0 for distance in distances):
        if distances[-1] >= distances[0]:
            return Decimal("1")
        peak = max(distances)
        if peak > 0:
            retained = _clamp_unit(distances[-1] / peak)
            return Decimal("0.70") + retained * Decimal("0.20")
        return Decimal("0.70")
    return correct_fraction * Decimal("0.60")


def _smart_stability_quality(
    ask_prices: list[Decimal],
    sample_count: int,
) -> Decimal:
    if sample_count < 2 or len(ask_prices) < sample_count:
        return Decimal("0")
    recent = ask_prices[-sample_count:]
    price_range = max(recent) - min(recent)
    return _clamp_unit(Decimal("1") - price_range / Decimal("0.10"))


def choose_smart_score_signal(
    market: Market,
    probability_up: Decimal,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    decision_seconds_before_end: Decimal,
    min_seconds_before_end: Decimal,
    min_entry: Decimal,
    max_entry: Decimal,
    edge_threshold: Decimal,
    max_spread: Decimal,
    min_ask_sum: Decimal,
    max_ask_sum: Decimal,
    recent_spot_prices: list[Decimal],
    start_price: Decimal,
    up_ask_prices: list[Decimal],
    down_ask_prices: list[Decimal],
    score_threshold: Decimal = Decimal("70"),
    min_probability: Decimal = Decimal("0.52"),
    fee_rate: Decimal = Decimal("0.07"),
    assumed_slippage: Decimal = Decimal("0.01"),
    trend_samples: int = 3,
    stability_samples: int = 3,
    probability_shrinkage: Decimal = Decimal("1"),
) -> AutoTradeSignal | None:
    if (
        seconds_to_end > decision_seconds_before_end
        or seconds_to_end < min_seconds_before_end
    ):
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
    assert up_quote is not None and up_quote.bid is not None and up_quote.ask is not None
    assert down_quote is not None and down_quote.bid is not None and down_quote.ask is not None

    calibrated_up = shrink_probability_toward_even(
        probability_up,
        probability_shrinkage,
    )
    candidates = (
        ("UP", market.token_ids[0], up_quote, calibrated_up, up_ask_prices),
        (
            "DOWN",
            market.token_ids[1],
            down_quote,
            Decimal("1") - calibrated_up,
            down_ask_prices,
        ),
    )
    ranked: list[
        tuple[Decimal, str, str, OrderBookQuote, Decimal, Decimal, list[Decimal]]
    ] = []
    for side, token_id, quote, probability, ask_prices in candidates:
        execution_entry = quote.ask + assumed_slippage
        fee_per_share = (
            fee_rate * execution_entry * (Decimal("1") - execution_entry)
        )
        net_edge = probability - execution_entry - fee_per_share
        ranked.append(
            (
                net_edge,
                side,
                token_id,
                quote,
                probability,
                execution_entry,
                ask_prices,
            )
        )
    (
        net_edge,
        side,
        token_id,
        quote,
        selected_probability,
        entry,
        selected_ask_prices,
    ) = max(ranked, key=lambda item: item[0])
    if (
        entry < min_entry
        or entry > max_entry
        or selected_probability < min_probability
        or net_edge <= 0
    ):
        return None

    required_edge = required_fair_value_edge(entry, seconds_to_end, edge_threshold)
    edge_quality = _clamp_unit(
        net_edge / max(required_edge, Decimal("0.01"))
    )
    trend_quality = _smart_trend_quality(
        recent_spot_prices,
        start_price,
        side,
        trend_samples,
    )
    selected_spread = quote.ask - quote.bid
    spread_quality = (
        _clamp_unit(Decimal("1") - selected_spread / max_spread)
        if max_spread > 0
        else Decimal("1")
    )
    ask_sum = up_quote.ask + down_quote.ask
    ask_sum_radius = max(
        Decimal("1") - min_ask_sum,
        max_ask_sum - Decimal("1"),
    )
    ask_sum_quality = (
        _clamp_unit(Decimal("1") - abs(ask_sum - Decimal("1")) / ask_sum_radius)
        if ask_sum_radius > 0
        else Decimal("1")
    )
    market_quality = spread_quality * Decimal("0.60") + ask_sum_quality * Decimal(
        "0.40"
    )
    stability_quality = _smart_stability_quality(
        selected_ask_prices,
        stability_samples,
    )
    timing_span = decision_seconds_before_end - min_seconds_before_end
    timing_quality = (
        _clamp_unit((seconds_to_end - min_seconds_before_end) / timing_span)
        if timing_span > 0
        else Decimal("1")
    )
    breakdown = SmartScoreBreakdown(
        total=(
            edge_quality * Decimal("45")
            + trend_quality * Decimal("20")
            + market_quality * Decimal("15")
            + stability_quality * Decimal("15")
            + timing_quality * Decimal("5")
        ),
        required=(
            score_threshold
            + (Decimal("5") if seconds_to_end < Decimal("45") else Decimal("0"))
            + (
                _clamp_unit(
                    (entry - Decimal("0.65")) / Decimal("0.13")
                )
                * Decimal("5")
                if entry > Decimal("0.65")
                else Decimal("0")
            )
        ),
        edge=edge_quality * Decimal("45"),
        trend=trend_quality * Decimal("20"),
        market=market_quality * Decimal("15"),
        stability=stability_quality * Decimal("15"),
        timing=timing_quality * Decimal("5"),
    )
    logger.info(
        "SMART_SCORE side=%s total=%s required=%s eligible=%s "
        "entry=%s probability=%s net_edge=%s "
        "components=edge:%s,trend:%s,market:%s,stability:%s,timing:%s",
        side,
        breakdown.total.quantize(Decimal("0.01")),
        breakdown.required.quantize(Decimal("0.01")),
        breakdown.total >= breakdown.required,
        entry,
        selected_probability.quantize(Decimal("0.0001")),
        net_edge.quantize(Decimal("0.0001")),
        breakdown.edge.quantize(Decimal("0.01")),
        breakdown.trend.quantize(Decimal("0.01")),
        breakdown.market.quantize(Decimal("0.01")),
        breakdown.stability.quantize(Decimal("0.01")),
        breakdown.timing.quantize(Decimal("0.01")),
    )
    if breakdown.total < breakdown.required:
        return None

    return AutoTradeSignal(
        side=side,
        token_id=token_id,
        price=entry,
        reason=(
            f"smart_score total={breakdown.total.quantize(Decimal('0.01'))} "
            f"required={breakdown.required.quantize(Decimal('0.01'))} "
            f"quoted_ask={quote.ask} entry={entry} "
            f"probability={selected_probability.quantize(Decimal('0.0001'))} "
            f"net_edge={net_edge.quantize(Decimal('0.0001'))} "
            f"components=edge:{breakdown.edge.quantize(Decimal('0.01'))},"
            f"trend:{breakdown.trend.quantize(Decimal('0.01'))},"
            f"market:{breakdown.market.quantize(Decimal('0.01'))},"
            f"stability:{breakdown.stability.quantize(Decimal('0.01'))},"
            f"timing:{breakdown.timing.quantize(Decimal('0.01'))} "
            f"seconds_left={int(seconds_to_end)}"
        ),
    )


def one_way_trend_side(
    up_ask_prices: list[Decimal],
    down_ask_prices: list[Decimal],
    sample_count: int,
) -> str | None:
    """Return the side whose ask rises while the opposite ask does not rise."""
    if (
        sample_count < 2
        or len(up_ask_prices) < sample_count
        or len(down_ask_prices) < sample_count
    ):
        return None
    recent_up = up_ask_prices[-sample_count:]
    recent_down = down_ask_prices[-sample_count:]
    if all(
        current >= previous for previous, current in zip(recent_up, recent_up[1:])
    ) and all(
        current <= previous for previous, current in zip(recent_down, recent_down[1:])
    ):
        return "UP"
    if all(
        current >= previous for previous, current in zip(recent_down, recent_down[1:])
    ) and all(
        current <= previous for previous, current in zip(recent_up, recent_up[1:])
    ):
        return "DOWN"
    return None


def choose_one_way_trend_signal(
    market: Market,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    spot_price: Decimal,
    start_price: Decimal,
    up_ask_prices: list[Decimal],
    down_ask_prices: list[Decimal],
    entry_start_seconds: Decimal,
    entry_cutoff_seconds: Decimal,
    min_entry: Decimal,
    max_entry: Decimal,
    trend_samples: int,
    max_spread: Decimal,
    min_ask_sum: Decimal,
    max_ask_sum: Decimal,
) -> AutoTradeSignal | None:
    if not entry_cutoff_seconds <= seconds_to_end <= entry_start_seconds:
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
    side = one_way_trend_side(up_ask_prices, down_ask_prices, trend_samples)
    if side is None:
        return None
    if (side == "UP" and spot_price <= start_price) or (
        side == "DOWN" and spot_price >= start_price
    ):
        return None
    quote = up_quote if side == "UP" else down_quote
    if quote.ask is None or quote.ask < min_entry or quote.ask > max_entry:
        return None
    token_id = market.token_ids[0] if side == "UP" else market.token_ids[1]
    return AutoTradeSignal(
        side=side,
        token_id=token_id,
        price=quote.ask,
        reason=(
            f"one_way_trend entry={quote.ask} samples={trend_samples} "
            f"spot={spot_price} official_open={start_price} "
            f"seconds_left={int(seconds_to_end)}"
        ),
    )


def choose_open_reversal_stop_signal(
    market: Market,
    primary_side: str,
    spot_price: Decimal,
    start_price: Decimal,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    entry_cutoff_seconds: Decimal,
    minimum_cross_usd: Decimal,
    minimum_bid: Decimal,
    max_entry: Decimal,
    max_spread: Decimal,
    min_ask_sum: Decimal,
    max_ask_sum: Decimal,
) -> AutoTradeSignal | None:
    if seconds_to_end < entry_cutoff_seconds or primary_side not in {"UP", "DOWN"}:
        return None
    side = "DOWN" if primary_side == "UP" else "UP"
    crossed_open = (
        spot_price <= start_price - minimum_cross_usd
        if side == "DOWN"
        else spot_price >= start_price + minimum_cross_usd
    )
    if not crossed_open:
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
    quote = up_quote if side == "UP" else down_quote
    if (
        quote.bid is None
        or quote.bid < minimum_bid
        or quote.ask is None
        or quote.ask <= 0
        or quote.ask > max_entry
    ):
        return None
    token_id = market.token_ids[0] if side == "UP" else market.token_ids[1]
    return AutoTradeSignal(
        side=side,
        token_id=token_id,
        price=quote.ask,
        reason=(
            f"protective_open_reversal_stop primary_side={primary_side} entry={quote.ask} "
            f"bid={quote.bid} spot={spot_price} official_open={start_price} "
            f"cross_buffer={minimum_cross_usd.quantize(Decimal('0.01'))} "
            f"seconds_left={int(seconds_to_end)}"
        ),
    )


def spot_reversed_across_open(
    primary_side: str,
    spot_price: Decimal,
    start_price: Decimal,
    buffer_usd: Decimal = Decimal("0"),
) -> bool:
    if primary_side == "UP":
        return spot_price <= start_price - buffer_usd
    if primary_side == "DOWN":
        return spot_price >= start_price + buffer_usd
    return False


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


def advance_signal_confirmation(
    state: SignalConfirmationState,
    signal: AutoTradeSignal,
    observed_at: float,
    required_confirmations: int,
    minimum_duration_seconds: float = 0.0,
    maximum_price_worsening: Decimal | None = None,
) -> tuple[bool, str]:
    if required_confirmations < 1 or minimum_duration_seconds < 0:
        raise ValueError("Confirmation count must be positive and duration non-negative")
    if maximum_price_worsening is not None and maximum_price_worsening < 0:
        raise ValueError("Maximum price worsening must not be negative")

    starts_new_sequence = state.side != signal.side or state.started_at is None
    if (
        not starts_new_sequence
        and maximum_price_worsening is not None
        and state.initial_price is not None
        and signal.price - state.initial_price > maximum_price_worsening
    ):
        starts_new_sequence = True

    if starts_new_sequence:
        state.side = signal.side
        state.confirmations = 1
        state.started_at = observed_at
        state.initial_price = signal.price
    else:
        state.confirmations += 1

    elapsed = max(
        0.0,
        observed_at - (state.started_at if state.started_at is not None else observed_at),
    )
    ready = state.confirmations >= required_confirmations and elapsed >= minimum_duration_seconds
    if ready:
        return True, "confirmed"
    return (
        False,
        f"confirmations={state.confirmations}/{required_confirmations} "
        f"duration={elapsed:.1f}/{minimum_duration_seconds:.1f}s",
    )


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


def live_response_is_matched(response: Any, *, require_fill_amounts: bool = False) -> bool:
    matched = (
        isinstance(response, dict)
        and response.get("success") is True
        and str(response.get("status", "")).lower() == "matched"
        and bool(response.get("orderID"))
    )
    if not matched or not require_fill_amounts:
        return matched
    try:
        return (
            Decimal(str(response.get("makingAmount") or "0")) > 0
            and Decimal(str(response.get("takingAmount") or "0")) > 0
        )
    except (ArithmeticError, ValueError):
        return False


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
    trader = ClobTradingClient(
        host=args.clob_host,
        chain_id=args.chain_id,
        private_key=private_key,
        funder_address=funder_address,
        signature_type=args.signature_type,
    )
    trader.prewarm_order_submission()
    return trader


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
    if args.disable_telegram_commands:
        os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    if args.disable_discord:
        os.environ["DISCORD_ENABLED"] = "false"
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
    open_price_tracker = StableOpenPriceTracker(
        required_confirmations=args.official_open_confirmations,
        minimum_stable_seconds=args.official_open_stable_seconds,
    )
    last_spot_price: Decimal | None = None
    last_spot_fetched_at: float | None = None
    prices: list[Decimal] = []
    price_sample_times: list[float] = []
    up_ask_prices: list[Decimal] = []
    down_ask_prices: list[Decimal] = []
    open_060_previous_up_ask = Decimal("0.50")
    open_060_previous_down_ask = Decimal("0.50")
    open_060_reference_spot: Decimal | None = None
    signals_this_window = 0
    confirmation_state = SignalConfirmationState()
    one_way_reversal_started_at: float | None = None
    primary_side_this_window: str | None = None
    primary_cost_this_window = Decimal("0")
    primary_shares_this_window = Decimal("0")
    primary_orders_this_window = 0
    aggregate_protection_completed = False
    edge_threshold = Decimal(args.edge)
    fallback_sigma = Decimal(args.fallback_sigma)
    confirmation_jump_sigma_multiplier = Decimal(args.confirmation_jump_sigma_multiplier)
    confirmation_min_jump_usd = Decimal(args.confirmation_min_jump_usd)
    trend_pullback_tolerance_usd = Decimal(args.trend_pullback_tolerance_usd)
    trend_pullback_tolerance_percent = Decimal(args.trend_pullback_tolerance_percent)
    one_way_entry_seconds = Decimal(str(args.one_way_entry_seconds))
    one_way_entry_cutoff_seconds = Decimal(str(args.one_way_entry_cutoff_seconds))
    one_way_min_entry = Decimal(args.one_way_min_entry)
    one_way_max_entry = Decimal(args.one_way_max_entry)
    one_way_reversal_seconds = args.one_way_reversal_seconds
    one_way_reversal_early_seconds = args.one_way_reversal_early_seconds
    one_way_reversal_final_window_seconds = Decimal(
        str(args.one_way_reversal_final_window_seconds)
    )
    one_way_reversal_min_usd = Decimal(args.one_way_reversal_min_usd)
    one_way_reversal_min_bid = Decimal(args.one_way_reversal_min_bid)
    one_way_reversal_max_entry = Decimal(args.one_way_reversal_max_entry)
    one_way_reversal_min_loss_reduction_percent = Decimal(
        args.one_way_reversal_min_loss_reduction_percent
    )
    one_way_reversal_min_loss_reduction_notional = Decimal(
        args.one_way_reversal_min_loss_reduction_notional
    )
    hedge_confirmation_min_seconds = args.hedge_confirmation_min_seconds
    hedge_max_price_worsening = Decimal(args.hedge_max_price_worsening)
    hedge_min_win_probability = Decimal(args.hedge_min_win_probability)
    hedge_min_edge = Decimal(args.hedge_min_edge)
    hedge_fee_rate = Decimal(args.hedge_fee_rate)
    hedge_entry_start_seconds = Decimal(str(args.hedge_entry_start_seconds))
    hedge_entry_cutoff_seconds = Decimal(str(args.hedge_entry_cutoff_seconds))
    hedge_open_cross_min_usd = Decimal(args.hedge_open_cross_min_usd)
    hedge_open_cross_sigma_multiplier = Decimal(args.hedge_open_cross_sigma_multiplier)
    hedge_market_reversal_threshold = Decimal(args.hedge_market_reversal_threshold)
    hedge_max_entry = Decimal(args.hedge_max_entry)
    hedge_max_spread = Decimal(args.hedge_max_spread)
    hedge_max_live_notional = Decimal(args.hedge_max_live_notional)
    min_entry = Decimal(args.min_entry)
    max_entry = Decimal(args.max_entry)
    max_spread = Decimal(args.max_spread)
    min_ask_sum = Decimal(args.min_ask_sum)
    max_ask_sum = Decimal(args.max_ask_sum)
    min_win_probability = Decimal(args.min_win_probability)
    probability_shrinkage = Decimal(args.probability_shrinkage)
    smart_score_threshold = Decimal(args.smart_score_threshold)
    smart_score_entry_seconds = Decimal(str(args.smart_score_entry_seconds))
    smart_score_cutoff_seconds = Decimal(str(args.smart_score_cutoff_seconds))
    smart_score_min_probability = Decimal(args.smart_score_min_probability)
    smart_score_fee_rate = Decimal(args.smart_score_fee_rate)
    smart_score_slippage = Decimal(args.smart_score_slippage)
    open_060_entry_seconds = Decimal(str(args.open_060_entry_seconds))
    open_060_cutoff_seconds = Decimal(str(args.open_060_cutoff_seconds))
    open_060_target = Decimal(args.open_060_target)
    open_060_slippage = Decimal(args.open_060_slippage)
    open_060_fee_rate = Decimal(args.open_060_fee_rate)
    open_060_initial_ask = Decimal(args.open_060_initial_ask)
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
    live_buy_slippage = Decimal(args.live_buy_slippage)
    pre_submit_max_adverse_ask_drop = Decimal(args.pre_submit_max_adverse_ask_drop)
    pre_submit_max_ask_worsening = Decimal(args.pre_submit_max_ask_worsening)
    pre_submit_max_quote_age_seconds = args.pre_submit_max_quote_age_seconds
    max_live_notional = Decimal(args.max_live_notional)
    late_max_live_notional = Decimal(args.late_max_live_notional)
    decision_seconds_before_end = Decimal(str(args.decision_seconds_before_end))
    min_seconds_before_end = Decimal(str(args.min_seconds_before_end))
    paper_bankroll = Decimal(args.paper_bankroll)
    paper_stake = Decimal(args.paper_stake)
    paper_shares = Decimal(args.paper_shares)
    paper_positions: list[PaperPosition] = []
    consecutive_losses = 0
    pause_windows_remaining = 0
    risk_pause_active_for_window = False
    live_orders_submitted = 0
    live_orders_matched = 0
    reversal_runtime: ReversalRuntime | None = None
    reversal_daily_restart_pending = False
    live_summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "mode": "live" if args.live_trading else "paper" if args.paper_trading else "dry_run",
        "status": "running",
        "strategy": args.strategy,
        "primary_signal_confirmations": primary_signal_confirmation_count(
            args.strategy,
            args.signal_confirmations,
        ),
        "max_live_orders": args.max_live_orders,
        "max_trades_per_window": strategy_trade_limit(args.strategy, args.max_trades),
        "max_primary_trades_per_window": args.max_trades,
        "max_matched_orders_per_window": strategy_trade_limit(args.strategy, args.max_trades),
        "max_live_notional": str(max_live_notional),
        "hedge_max_live_notional": (
            str(hedge_max_live_notional)
            if hedge_max_live_notional > 0
            else "dynamic_primary_cost"
        ),
        "late_max_live_notional": str(late_max_live_notional),
        "probability_shrinkage": str(probability_shrinkage),
        "smart_score_threshold": str(smart_score_threshold),
        "smart_score_entry_seconds": str(smart_score_entry_seconds),
        "smart_score_cutoff_seconds": str(smart_score_cutoff_seconds),
        "smart_score_min_probability": str(smart_score_min_probability),
        "smart_score_fee_rate": str(smart_score_fee_rate),
        "smart_score_slippage": str(smart_score_slippage),
        "smart_score_trend_samples": args.smart_score_trend_samples,
        "smart_score_stability_samples": args.smart_score_stability_samples,
        "open_060_entry_seconds": str(open_060_entry_seconds),
        "open_060_cutoff_seconds": str(open_060_cutoff_seconds),
        "open_060_target": str(open_060_target),
        "open_060_slippage": str(open_060_slippage),
        "open_060_fee_rate": str(open_060_fee_rate),
        "trend_pullback_tolerance_usd": str(trend_pullback_tolerance_usd),
        "trend_pullback_tolerance_percent": str(trend_pullback_tolerance_percent),
        "one_way_entry_seconds": str(one_way_entry_seconds),
        "one_way_entry_cutoff_seconds": str(one_way_entry_cutoff_seconds),
        "one_way_entry_range": [str(one_way_min_entry), str(one_way_max_entry)],
        "one_way_trend_samples": args.one_way_trend_samples,
        "one_way_reversal_seconds": one_way_reversal_seconds,
        "one_way_reversal_early_seconds": one_way_reversal_early_seconds,
        "one_way_reversal_final_window_seconds": str(one_way_reversal_final_window_seconds),
        "one_way_reversal_min_usd": str(one_way_reversal_min_usd),
        "one_way_reversal_min_bid": str(one_way_reversal_min_bid),
        "one_way_reversal_max_entry": str(one_way_reversal_max_entry),
        "one_way_primary_shares": str(order_size),
        "one_way_protection_shares": str(order_size),
        "one_way_reversal_min_loss_reduction_percent": str(
            one_way_reversal_min_loss_reduction_percent
        ),
        "one_way_reversal_min_loss_reduction_notional": str(
            one_way_reversal_min_loss_reduction_notional
        ),
        "order_type": args.live_order_type,
        "live_buy_slippage": str(live_buy_slippage),
        "post_fill_poll_interval": args.post_fill_poll_interval,
        "pre_submit_max_adverse_ask_drop": str(pre_submit_max_adverse_ask_drop),
        "pre_submit_max_ask_worsening": str(pre_submit_max_ask_worsening),
        "pre_submit_max_quote_age_seconds": pre_submit_max_quote_age_seconds,
        "max_spread": str(max_spread),
        "hedge_max_spread": str(hedge_max_spread),
        "hedge_signal_confirmations": args.hedge_signal_confirmations,
        "hedge_confirmation_min_seconds": hedge_confirmation_min_seconds,
        "hedge_max_price_worsening": str(hedge_max_price_worsening),
        "hedge_min_edge": str(hedge_min_edge),
        "hedge_entry_start_seconds": str(hedge_entry_start_seconds),
        "hedge_entry_cutoff_seconds": str(hedge_entry_cutoff_seconds),
        "hedge_open_cross_min_usd": str(hedge_open_cross_min_usd),
        "hedge_open_cross_sigma_multiplier": str(hedge_open_cross_sigma_multiplier),
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
        if args.paper_trading and (
            paper_bankroll <= 0
            or paper_stake <= 0
            or paper_shares < 0
        ):
            raise ValueError(
                "Paper bankroll and fallback stake must be positive; paper shares cannot be negative"
            )
        if args.live_trading and args.strategy not in LIVE_STRATEGIES:
            raise ValueError(f"{args.strategy} is not approved for live strategy selection")
        if args.strategy == "late_one_way" and args.max_trades < 2:
            raise ValueError("late_one_way requires two per-window slots for its reversal stop")
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
        if (
            not Decimal("0") <= smart_score_threshold <= Decimal("100")
            or smart_score_cutoff_seconds < 0
            or smart_score_cutoff_seconds >= smart_score_entry_seconds
            or not Decimal("0.5") <= smart_score_min_probability <= Decimal("1")
            or smart_score_fee_rate < 0
            or smart_score_slippage < 0
            or args.smart_score_trend_samples < 1
            or args.smart_score_stability_samples < 2
        ):
            raise ValueError("Smart-score thresholds, costs, and sample counts must be valid")
        if (
            open_060_cutoff_seconds < 0
            or open_060_cutoff_seconds >= open_060_entry_seconds
            or not Decimal("0") < open_060_initial_ask < open_060_target < Decimal("1")
            or open_060_slippage < 0
            or open_060_target + open_060_slippage >= Decimal("1")
            or open_060_fee_rate < 0
        ):
            raise ValueError("Open-0.60 timing, price, slippage, and fee parameters must be valid")
        if fallback_sigma <= 0:
            raise ValueError("Fallback sigma must be positive")
        if args.trend_confirmation_samples < 1:
            raise ValueError("Trend confirmation samples must be positive")
        if (
            args.one_way_trend_samples < 2
            or one_way_entry_cutoff_seconds < 0
            or one_way_entry_cutoff_seconds >= one_way_entry_seconds
            or one_way_reversal_seconds < 0
            or one_way_reversal_early_seconds < 0
            or one_way_reversal_final_window_seconds < 0
            or one_way_reversal_min_usd < 0
        ):
            raise ValueError("One-way sampling, entry timing, and reversal duration must be valid")
        if not Decimal("0") < one_way_min_entry <= one_way_max_entry < Decimal("1"):
            raise ValueError("One-way entry range must be within zero and one")
        if not (
            Decimal("0") <= one_way_reversal_min_bid <= one_way_reversal_max_entry < Decimal("1")
        ):
            raise ValueError("One-way reversal bid and entry limits must be within zero and one")
        if not Decimal("0") <= one_way_reversal_min_loss_reduction_percent <= Decimal("1"):
            raise ValueError("One-way reversal loss-reduction percent must be between zero and one")
        if one_way_reversal_min_loss_reduction_notional < 0:
            raise ValueError("One-way reversal minimum loss reduction must not be negative")
        if trend_pullback_tolerance_usd < 0 or trend_pullback_tolerance_percent < 0:
            raise ValueError("Trend pullback tolerance must not be negative")
        if args.hedge_signal_confirmations < 2:
            raise ValueError("Hedge signal confirmations must be at least two")
        if hedge_confirmation_min_seconds < 0 or hedge_max_price_worsening < 0:
            raise ValueError("Hedge confirmation duration and price worsening must not be negative")
        if hedge_open_cross_min_usd < 0 or hedge_open_cross_sigma_multiplier < 0:
            raise ValueError("Hedge open-cross thresholds must not be negative")
        if hedge_entry_cutoff_seconds < 0 or hedge_entry_cutoff_seconds >= hedge_entry_start_seconds:
            raise ValueError("Hedge entry cutoff must be non-negative and lower than its start")
        if not Decimal("0.5") < hedge_market_reversal_threshold < Decimal("1"):
            raise ValueError("Hedge market-reversal threshold must be between 0.5 and 1")
        if not Decimal("0") < hedge_max_entry < Decimal("1"):
            raise ValueError("Hedge maximum entry must be between zero and one")
        if hedge_max_spread < 0:
            raise ValueError("Hedge maximum spread must not be negative")
        if hedge_max_live_notional < 0:
            raise ValueError("Hedge maximum live notional must not be negative")
        if (
            args.final_poll_seconds < 0
            or args.final_poll_interval <= 0
            or args.post_fill_poll_interval <= 0
        ):
            raise ValueError("Final polling window must be non-negative and interval positive")
        if not Decimal("0") <= hedge_min_win_probability <= Decimal("1"):
            raise ValueError("Hedge minimum win probability must be between zero and one")
        if not Decimal("0") <= hedge_min_edge <= Decimal("1"):
            raise ValueError("Hedge minimum edge must be between zero and one")
        if hedge_fee_rate < 0:
            raise ValueError("Hedge fee rate must be non-negative")
        if confirmation_jump_sigma_multiplier < 0 or confirmation_min_jump_usd < 0:
            raise ValueError("Confirmation jump thresholds must be non-negative")
        if args.low_entry_confirmation_samples < 1:
            raise ValueError("Low-entry confirmation samples must be positive")
        if max_price_alignment_difference < 0:
            raise ValueError("Maximum price-alignment difference must be non-negative")
        if live_buy_slippage < 0:
            raise ValueError("Live buy slippage must not be negative")
        if pre_submit_max_adverse_ask_drop < 0:
            raise ValueError("Pre-submit adverse ask drop must not be negative")
        if pre_submit_max_ask_worsening < 0:
            raise ValueError("Pre-submit ask worsening must not be negative")
        if pre_submit_max_quote_age_seconds <= 0:
            raise ValueError("Pre-submit quote age must be positive")
        if args.max_boundary_sample_offset_ms < 0:
            raise ValueError("Maximum boundary-sample offset must be non-negative")
        if args.official_open_confirmations < 2:
            raise ValueError("Official open confirmations must be at least two")
        if args.official_open_stable_seconds < 0:
            raise ValueError("Official open stable seconds must not be negative")
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
        if args.live_trading or args.strategy == "reversal_v11":
            reversal_state_path = Path(args.reversal_state_json)
            reversal_strategy = ReversalV11.load(reversal_state_path)
            active_reversal = reversal_strategy.state.active_round
            prepared_reversal = reversal_strategy.state.prepared_split
            if (
                args.live_trading
                and (
                    (
                        active_reversal is not None
                        and (
                            active_reversal.execution_phase
                            in {"split_submitting", "split_uncertain"}
                            or (
                                active_reversal.awaiting_window != current_market.slug
                                and active_reversal.execution_phase
                                != "trend_exit_complete"
                            )
                        )
                    )
                    or (
                        prepared_reversal is not None
                        and prepared_reversal.execution_phase
                        in {
                            "split_submitting",
                            "split_uncertain",
                            "merge_submitting",
                            "merge_uncertain",
                        }
                    )
                )
            ):
                raise ValueError(
                    "reversal_v11 split/merge outcome is uncertain; reconcile positions before restart"
                )
            reversal_splitter = None
            if args.live_trading:
                assert trader is not None
                startup_market = load_updown_market(gamma, slug)
                if startup_market is None:
                    raise ValueError("reversal_v11 startup could not load the current market")
                reversal_splitter = splitter_from_config(dict(os.environ))
                if (
                    prepared_reversal is not None
                    and prepared_reversal.execution_phase == "split_confirmed"
                ):
                    required_reversal_collateral = Decimal("0")
                elif active_reversal is None:
                    required_reversal_collateral = Decimal("30")
                elif active_reversal.awaiting_window is not None and active_reversal.execution_phase in {
                    "split_confirmed",
                    "trend_exit_partial",
                    "trend_exit_submitting",
                    "trend_exit_complete",
                }:
                    required_reversal_collateral = Decimal("0")
                else:
                    required_reversal_collateral = reversal_strategy.settings.stakes[
                        active_reversal.failures
                    ]
                startup_report = reversal_startup_self_check(
                    market=startup_market,
                    splitter=reversal_splitter,
                    trader=trader,
                    signature_type=args.signature_type,
                    required_collateral=required_reversal_collateral,
                )
                live_summary["reversal_startup_self_check"] = {
                    "wallet": startup_report.wallet,
                    "collateral_units": startup_report.collateral_units,
                    "open_orders": startup_report.open_orders,
                    "up_balance": str(startup_report.up_balance),
                    "down_balance": str(startup_report.down_balance),
                    "relayer_deployed": startup_report.relayer_deployed,
                }
            reversal_runtime = ReversalRuntime(
                strategy=reversal_strategy,
                state_path=reversal_state_path,
                winner_lookup=fetch_winner,
                splitter=reversal_splitter,
                trader=trader,
                signature_type=args.signature_type,
                live=args.live_trading,
                order_callback=notifications.record_reversal_exit,
            )
    except Exception as exc:
        live_summary["error"] = f"{type(exc).__name__}: {exc}"
        notifications.notify_exception("启动检查或钱包签名", exc, key="startup", cooldown=0)
        notifications.stop("启动失败", exc)
        raise

    notifications.start()
    write_live_summary(finalize=False)
    if args.live_trading:
        logger.warning("LIVE TRADING ENABLED. Orders may be submitted.")
    elif args.paper_trading:
        logger.info(
            "PAPER TRADING mode. Starting bankroll=%s stake_per_signal=%s shares_per_signal=%s",
            paper_bankroll,
            paper_stake,
            paper_shares,
        )
    else:
        logger.info("DRY RUN mode. No orders will be submitted.")

    while time.time() < stop_at:
        iteration_started_at = time.monotonic()
        poll_interval = float(args.interval)
        now = datetime.now(timezone.utc)
        notifications.update_runtime()
        notifications.maybe_delete_expired_discord_messages()
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
        if reversal_runtime is not None and args.strategy == "reversal_v11":
            report_day = datetime.now(timezone.utc).date() - timedelta(days=1)
            if reversal_runtime.send_daily_report_once(
                report_day,
                notifications.send_strategy_report,
            ):
                reversal_daily_restart_pending = True
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
            open_price_tracker.reset()
            prices = []
            price_sample_times = []
            up_ask_prices = []
            down_ask_prices = []
            open_060_previous_up_ask = open_060_initial_ask
            open_060_previous_down_ask = open_060_initial_ask
            open_060_reference_spot = None
            signals_this_window = 0
            confirmation_state.reset()
            one_way_reversal_started_at = None
            primary_side_this_window = None
            primary_cost_this_window = Decimal("0")
            primary_shares_this_window = Decimal("0")
            primary_orders_this_window = 0
            aggregate_protection_completed = False
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
            reversal_round_active = False
            if reversal_runtime is not None and args.strategy == "reversal_v11":
                reversal_round_active = (
                    reversal_runtime.strategy.state.active_round is not None
                )
            activated_strategy = (
                None
                if reversal_round_active
                else notifications.activate_pending_strategy(current_market.slug)
            )
            if reversal_round_active and notifications.pending_strategy is not None:
                logger.info(
                    "REVERSAL_SWITCH_DEFERRED active_round=%s pending=%s",
                    reversal_runtime.strategy.state.active_round.round_id,
                    notifications.pending_strategy,
                )
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
        if primary_side_this_window is not None:
            poll_interval = min(poll_interval, args.post_fill_poll_interval)
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

        if (
            args.strategy == "open_060"
            or (
                args.strategy == "open_060_late_070"
                and seconds_to_end >= open_060_cutoff_seconds
            )
        ):
            if open_060_reference_spot is None:
                open_060_reference_spot = spot.price
            up_quote, down_quote = quote_outcomes(clob, current_market)
            signal = choose_open_060_signal(
                current_market,
                up_quote,
                down_quote,
                seconds_to_end,
                open_060_previous_up_ask,
                open_060_previous_down_ask,
                open_060_entry_seconds,
                open_060_cutoff_seconds,
                open_060_target,
                open_060_slippage,
                max_spread,
                min_ask_sum,
                max_ask_sum,
            )
            if up_quote is not None and up_quote.ask is not None:
                open_060_previous_up_ask = up_quote.ask
            if down_quote is not None and down_quote.ask is not None:
                open_060_previous_down_ask = down_quote.ask

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
                        start_spot=open_060_reference_spot,
                        spot_source=spot.source,
                        probability_up=Decimal("0.5"),
                        up_quote=up_quote,
                        down_quote=down_quote,
                    )
                )
            logger.info(
                "%s OPEN_060 seconds_left=%s up=%s down=%s previous_up=%s previous_down=%s",
                current_market.slug,
                int(seconds_to_end),
                up_quote,
                down_quote,
                open_060_previous_up_ask,
                open_060_previous_down_ask,
            )

            if (
                signal is not None
                and args.auto_trade
                and signals_this_window < strategy_trade_limit(args.strategy, args.max_trades)
                and not risk_pause_active_for_window
                and not notifications.trading_paused
            ):
                if trader is None:
                    logger.info(
                        "AUTO_SIGNAL %s side=%s price=%s size=%s reason=%s",
                        current_market.slug,
                        signal.side,
                        signal.price,
                        order_size,
                        signal.reason,
                    )
                    signals_this_window = window_trade_count_after_attempt(
                        signals_this_window,
                        live=False,
                    )
                if args.paper_trading:
                    paper_trade_stake = (
                        signal.price * paper_shares
                        if paper_shares > 0
                        else paper_stake
                    )
                    paper_bankroll = open_paper_position(
                        paper_positions,
                        paper_bankroll,
                        current_market.slug,
                        signal,
                        paper_trade_stake,
                        open_060_fee_rate,
                    )
                    if args.stop_when_bust and paper_bankroll <= 0:
                        logger.info("PAPER_BUST bankroll=%s. Exiting after open position.", paper_bankroll)
                        return
                elif trader is not None:
                    try:
                        refreshed_up_book, refreshed_down_book = clob.books(
                            current_market.token_ids
                        )
                    except Exception as exc:
                        logger.warning(
                            "ORDER_BLOCKED_PRE_SUBMIT_REFRESH slug=%s error=%s",
                            current_market.slug,
                            exc,
                        )
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    refreshed_signal = refresh_open_060_signal(
                        current_market,
                        signal.side,
                        refreshed_up_book.quote,
                        refreshed_down_book.quote,
                        max(
                            Decimal("0"),
                            _seconds_to_end(
                                current_market,
                                datetime.now(timezone.utc),
                            ),
                        ),
                        open_060_cutoff_seconds,
                        open_060_target,
                        open_060_slippage,
                        max_spread,
                        min_ask_sum,
                        max_ask_sum,
                    )
                    if refreshed_signal is None:
                        logger.info(
                            "ORDER_BLOCKED_SIGNAL_CHANGED slug=%s confirmed_side=%s latest_side=NONE",
                            current_market.slug,
                            signal.side,
                        )
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    signal = refreshed_signal
                    selected_book = (
                        refreshed_up_book
                        if signal.side == "UP"
                        else refreshed_down_book
                    )
                    refreshed_quote = selected_book.quote
                    assert refreshed_quote.ask is not None
                    quoted_ask = refreshed_quote.ask
                    available_depth = executable_ask_depth(
                        selected_book,
                        signal.price,
                    )
                    if available_depth < order_size:
                        logger.info(
                            "ORDER_BLOCKED_INSUFFICIENT_DEPTH slug=%s side=%s "
                            "limit=%s required=%s available=%s",
                            current_market.slug,
                            signal.side,
                            signal.price,
                            order_size,
                            available_depth,
                        )
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    signal = AutoTradeSignal(
                        side=signal.side,
                        token_id=signal.token_id,
                        price=signal.price,
                        reason=(
                            f"{signal.reason} "
                            f"pre_submit_depth={available_depth.quantize(Decimal('0.000001'))} "
                            f"quote_ttl={pre_submit_max_quote_age_seconds:g}s"
                        ),
                    )
                    notional = signal.price * order_size
                    if not live_session_should_continue(
                        live_orders_submitted,
                        args.max_live_orders,
                    ):
                        logger.warning(
                            "LIVE ORDER LIMIT reached=%s. Exiting.",
                            live_orders_submitted,
                        )
                        return
                    if notional > max_live_notional:
                        logger.warning(
                            "LIVE SIGNAL SKIPPED notional=%s exceeds hard cap=%s; continuing.",
                            notional,
                            max_live_notional,
                        )
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    if notifications.trading_paused:
                        logger.warning(
                            "LIVE ORDER blocked by Telegram trading pause before submission."
                        )
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    logger.info(
                        "AUTO_SIGNAL %s side=%s price=%s size=%s reason=%s",
                        current_market.slug,
                        signal.side,
                        signal.price,
                        order_size,
                        signal.reason,
                    )
                    order_record = {
                        "attempted_at": datetime.now(timezone.utc).isoformat(),
                        "slug": current_market.slug,
                        "side": signal.side,
                        "token_id": signal.token_id,
                        "price": str(signal.price),
                        "size": str(order_size),
                        "notional": str(notional),
                        "order_type": args.live_order_type,
                        "order_role": "primary",
                        "quoted_ask": str(quoted_ask),
                        "max_slippage": str(open_060_slippage),
                        "applied_slippage": str(signal.price - quoted_ask),
                        "reason": signal.reason,
                        "response": None,
                        "error": None,
                    }
                    live_summary["status"] = "submitting"
                    live_summary["order_attempts"] = live_orders_submitted + 1
                    live_summary["order"] = {
                        key: value
                        for key, value in order_record.items()
                        if key not in {"response", "error"}
                    }
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
                            submit_not_after_monotonic=(
                                time.monotonic()
                                + pre_submit_max_quote_age_seconds
                            ),
                        )
                    except OrderQuoteExpiredError as exc:
                        live_orders_submitted -= 1
                        live_summary["status"] = "running"
                        live_summary["order_attempts"] = live_orders_submitted
                        live_summary["error"] = None
                        order_record["error"] = f"{type(exc).__name__}: {exc}"
                        write_live_summary(finalize=False)
                        logger.info(
                            "ORDER_BLOCKED_QUOTE_EXPIRED slug=%s side=%s error=%s",
                            current_market.slug,
                            signal.side,
                            exc,
                        )
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
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
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    live_summary["response"] = response
                    order_record["response"] = response
                    logger.info("LIVE ORDER response=%s", response)
                    matched = live_response_is_matched(
                        response,
                        require_fill_amounts=args.live_order_type == "FAK",
                    )
                    signals_this_window = window_trade_count_after_attempt(
                        signals_this_window,
                        live=True,
                        matched=matched,
                    )
                    if not matched:
                        live_summary["status"] = "running"
                        live_summary["error"] = (
                            f"Order response was not conclusively matched: {response}"
                        )
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
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    live_orders_matched += 1
                    live_summary["matched_orders"] = live_orders_matched
                    order_record["matched_at"] = datetime.now(
                        timezone.utc
                    ).isoformat()
                    notifications.record_fill(order_record)
                    live_summary["status"] = "running"
                    write_live_summary(finalize=False)
                    logger.warning(
                        "LIVE ORDER attempt=%s matched_count=%s. Continuing; session_limit=%s.",
                        live_orders_submitted,
                        live_orders_matched,
                        (
                            args.max_live_orders
                            if args.max_live_orders > 0
                            else "unlimited"
                        ),
                    )
                else:
                    logger.info(
                        "DRY RUN: would buy %s at %s x %s",
                        signal.side,
                        signal.price,
                        order_size,
                    )
            sleep_until_next_poll(poll_interval, iteration_started_at)
            continue

        if start_price is None:
            elapsed_since_start = -seconds_to_start
            try:
                price_to_beat = price_to_beat_client.fetch(
                    current_market.event_start_time,
                    current_market.end_time,
                )
            except Exception as exc:
                open_price_tracker.reset()
                logger.warning(
                    "PRICE_ALIGNMENT_PENDING slug=%s error=%s",
                    current_market.slug,
                    exc,
                )
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            else:
                confirmed_open_price = open_price_tracker.observe(
                    price_to_beat.price_to_beat,
                    time.monotonic(),
                )
                if confirmed_open_price is None:
                    stable_for = (
                        0.0
                        if open_price_tracker.candidate_since is None
                        else max(0.0, time.monotonic() - open_price_tracker.candidate_since)
                    )
                    logger.info(
                        "PRICE_TO_BEAT_PENDING slug=%s candidate=%s confirmations=%s/%s "
                        "stable=%.1f/%.1fs endpoint_incomplete=%s",
                        current_market.slug,
                        open_price_tracker.candidate,
                        open_price_tracker.confirmations,
                        open_price_tracker.required_confirmations,
                        stable_for,
                        open_price_tracker.minimum_stable_seconds,
                        price_to_beat.incomplete,
                    )
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
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
                    price_to_beat.price_to_beat,
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
                        price_to_beat.price_to_beat,
                        boundary_spot.price if boundary_spot is not None else None,
                        alignment_difference,
                        max_price_alignment_difference,
                        boundary_offset_ms,
                    )
                start_price = confirmed_open_price
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
            price_sample_times = [time.monotonic()]
            logger.info("Captured price_to_beat=%s for %s", start_price, current_market.slug)
        else:
            prices.append(spot.price)
            price_sample_times.append(time.monotonic())

        sigma = estimate_sigma_per_sqrt_second(
            prices,
            Decimal(str(poll_interval)),
            fallback_sigma,
            price_sample_times,
        )
        fair = btc_up_probability(start_price, spot.price, max(Decimal("0"), seconds_to_end), sigma)
        up_quote, down_quote = quote_outcomes(clob, current_market)
        up_ask = up_quote.ask if up_quote else None
        down_ask = down_quote.ask if down_quote else None
        if up_ask is not None and down_ask is not None:
            up_ask_prices.append(up_ask)
            down_ask_prices.append(down_ask)
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

        if args.strategy == "reversal_v11":
            if reversal_runtime is None:
                error = RuntimeError("reversal_v11 runtime was not initialized")
                notifications.notify_exception(
                    "反转策略运行时",
                    error,
                    key="reversal-runtime-missing",
                    cooldown=0,
                )
                notifications._set_trading_paused(True)
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            try:
                reversal_state = reversal_runtime.strategy.state
                next_amount = reversal_runtime.strategy.opening_split_amount(
                    current_market.slug
                )
                up_book, down_book = clob.books(current_market.token_ids)
                reversal_health_by_side = {
                    side: market_health_from_books(
                        trend_side=side,
                        up_book=up_book,
                        down_book=down_book,
                        making_amount=next_amount,
                        spot_prices=prices,
                        open_price=start_price,
                    )
                    for side in (Direction.UP, Direction.DOWN)
                }
                reversal_result = reversal_runtime.tick(
                    market=current_market,
                    up_book=up_book,
                    down_book=down_book,
                    health_by_side=reversal_health_by_side,
                    book_refresh=lambda: clob.books(current_market.token_ids),
                )
                logger.info(
                    "REVERSAL_V11 slug=%s status=%s plan=%s detail=%s",
                    current_market.slug,
                    reversal_result.status,
                    reversal_result.plan,
                    reversal_result.detail,
                )
                if reversal_result.order is not None:
                    live_summary["orders"].append(reversal_result.order)
                    live_orders_submitted += 1
                    live_summary["order_attempts"] = live_orders_submitted
                    if live_response_is_matched(
                        reversal_result.order.get("response"),
                        require_fill_amounts=True,
                    ):
                        live_orders_matched += 1
                        live_summary["matched_orders"] = live_orders_matched
                    write_live_summary(finalize=False)
                if (
                    reversal_daily_restart_pending
                    and reversal_runtime.strategy.state.active_round is None
                    and reversal_runtime.strategy.state.prepared_split is None
                ):
                    live_summary["status"] = "daily_safe_restart"
                    write_live_summary()
                    notifications.stop("反转策略日报安全重启")
                    os.execv(
                        sys.executable,
                        [sys.executable, "-m", "src.watch_updown", *sys.argv[1:]],
                    )
            except Exception as exc:
                live_summary["error"] = f"{type(exc).__name__}: {exc}"
                notifications.notify_exception(
                    f"反转策略 {current_market.slug}",
                    exc,
                    key=f"reversal:{current_market.slug}",
                    cooldown=0,
                )
                active_reversal = reversal_runtime.strategy.state.active_round
                prepared_reversal = reversal_runtime.strategy.state.prepared_split
                if (
                    (
                        active_reversal is not None
                        and active_reversal.execution_phase
                        in {"split_submitting", "split_uncertain"}
                    )
                    or (
                        prepared_reversal is not None
                        and prepared_reversal.execution_phase
                        in {
                            "split_submitting",
                            "split_uncertain",
                            "merge_submitting",
                            "merge_uncertain",
                        }
                    )
                ):
                    notifications._set_trading_paused(True)
            sleep_until_next_poll(poll_interval, iteration_started_at)
            continue

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
            elif args.strategy == "late_one_way":
                if primary_side_this_window is None:
                    signal = choose_one_way_trend_signal(
                        current_market,
                        up_quote,
                        down_quote,
                        seconds_to_end,
                        spot.price,
                        start_price,
                        up_ask_prices,
                        down_ask_prices,
                        one_way_entry_seconds,
                        one_way_entry_cutoff_seconds,
                        one_way_min_entry,
                        one_way_max_entry,
                        args.one_way_trend_samples,
                        max_spread,
                        min_ask_sum,
                        max_ask_sum,
                    )
                else:
                    one_way_required_reversal_seconds = (
                        one_way_reversal_seconds
                        if seconds_to_end <= one_way_reversal_final_window_seconds
                        else one_way_reversal_early_seconds
                    )
                    one_way_reversal_buffer = protective_open_cross_buffer(
                        start_price,
                        fair.sigma_per_sqrt_second,
                        Decimal(str(one_way_required_reversal_seconds)),
                        one_way_reversal_min_usd,
                        hedge_open_cross_sigma_multiplier,
                    )
                    reversed_now = spot_reversed_across_open(
                        primary_side_this_window,
                        spot.price,
                        start_price,
                        one_way_reversal_buffer,
                    )
                    if not reversed_now:
                        one_way_reversal_started_at = None
                        confirmation_state.reset()
                        signal = None
                    else:
                        reversal_observed_at = time.monotonic()
                        if one_way_reversal_started_at is None:
                            one_way_reversal_started_at = reversal_observed_at
                        reversal_elapsed = reversal_observed_at - one_way_reversal_started_at
                        signal = (
                            choose_open_reversal_stop_signal(
                                current_market,
                                primary_side_this_window,
                                spot.price,
                                start_price,
                                up_quote,
                                down_quote,
                                seconds_to_end,
                                hedge_entry_cutoff_seconds,
                                one_way_reversal_buffer,
                                one_way_reversal_min_bid,
                                one_way_reversal_max_entry,
                                hedge_max_spread,
                                min_ask_sum,
                                max_ask_sum,
                            )
                            if reversal_elapsed >= one_way_required_reversal_seconds
                            else None
                        )
                        if signal is None:
                            confirmation_state.reset()
            elif args.strategy == "smart_score":
                signal = choose_smart_score_signal(
                    current_market,
                    fair.probability_up,
                    up_quote,
                    down_quote,
                    seconds_to_end,
                    smart_score_entry_seconds,
                    smart_score_cutoff_seconds,
                    min_entry,
                    max_entry,
                    edge_threshold,
                    max_spread,
                    min_ask_sum,
                    max_ask_sum,
                    prices,
                    start_price,
                    up_ask_prices,
                    down_ask_prices,
                    smart_score_threshold,
                    smart_score_min_probability,
                    smart_score_fee_rate,
                    smart_score_slippage,
                    args.smart_score_trend_samples,
                    args.smart_score_stability_samples,
                    probability_shrinkage,
                )
            else:
                protection_slot_reserved = (
                    primary_orders_this_window < args.max_trades
                    and (
                        trader is None
                        or args.max_live_orders == 0
                        or live_orders_submitted + 2 <= args.max_live_orders
                    )
                )
                normal_signal = (
                    choose_fair_value_edge_signal(
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
                    if not aggregate_protection_completed and protection_slot_reserved
                    else None
                )
                signal = normal_signal
                if primary_side_this_window is not None and not aggregate_protection_completed:
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
                        hedge_min_edge,
                        hedge_min_win_probability,
                        hedge_max_spread,
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
                        hedge_max_spread,
                        min_ask_sum,
                        max_ask_sum,
                    )
                    protective_signal = market_protective_signal or model_protective_signal
                    if protective_signal is not None:
                        open_cross_buffer = protective_open_cross_buffer(
                            start_price,
                            fair.sigma_per_sqrt_second,
                            Decimal(str(hedge_confirmation_min_seconds)),
                            hedge_open_cross_min_usd,
                            hedge_open_cross_sigma_multiplier,
                        )
                        if not protective_spot_confirms_open_cross(
                            prices,
                            start_price,
                            protective_signal.side,
                            open_cross_buffer,
                        ):
                            logger.info(
                                "HEDGE_PENDING_OPEN_CROSS side=%s spot=%s open=%s buffer=%s",
                                protective_signal.side,
                                spot.price,
                                start_price,
                                open_cross_buffer.quantize(Decimal("0.01")),
                            )
                            protective_signal = None
                        else:
                            protective_signal = AutoTradeSignal(
                                side=protective_signal.side,
                                token_id=protective_signal.token_id,
                                price=protective_signal.price,
                                reason=(
                                    f"{protective_signal.reason} "
                                    f"open_cross_buffer={open_cross_buffer.quantize(Decimal('0.01'))} "
                                    f"spot={spot.price} official_open={start_price}"
                                ),
                            )
                    if protective_signal is not None:
                        signal = protective_signal
                    elif normal_signal is None or normal_signal.side != primary_side_this_window:
                        signal = None
                elif aggregate_protection_completed:
                    signal = None
            if signal is not None:
                if notifications.trading_paused:
                    logger.warning("AUTO_SIGNAL blocked because Telegram trading pause is active.")
                    confirmation_state.reset()
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                if spot_age > Decimal(str(args.max_spot_age)):
                    logger.info("SIGNAL_REJECTED stale_spot_age=%.1fs", float(spot_age))
                    confirmation_state.reset()
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                is_protective_hedge = signal.reason.startswith("protective_")
                if (
                    is_fair_value_strategy(args.strategy)
                    and not is_protective_hedge
                    and signal.reason.startswith("fair_value_edge")
                    and not recent_spot_samples_support_side(
                        prices,
                        start_price,
                        signal.side,
                        args.trend_confirmation_samples,
                        (
                            trend_pullback_tolerance_usd
                            if signal.price >= low_entry_cutoff
                            else Decimal("0")
                        ),
                        (
                            trend_pullback_tolerance_percent
                            if signal.price >= low_entry_cutoff
                            else Decimal("0")
                        ),
                    )
                ):
                    effective_tolerance = effective_pullback_tolerance(
                        trend_pullback_tolerance_usd,
                        max(abs(price - start_price) for price in prices[-args.trend_confirmation_samples:]),
                        trend_pullback_tolerance_percent,
                    ) if signal.price >= low_entry_cutoff else Decimal("0")
                    logger.info(
                        "SIGNAL_REJECTED side=%s reason=recent_spot_distance_narrowing "
                        "samples=%s tolerance_usd=%s tolerance_percent=%s effective_tolerance_usd=%s",
                        signal.side,
                        args.trend_confirmation_samples,
                        (
                            trend_pullback_tolerance_usd
                            if signal.price >= low_entry_cutoff
                            else Decimal("0")
                        ),
                        (
                            trend_pullback_tolerance_percent
                            if signal.price >= low_entry_cutoff
                            else Decimal("0")
                        ),
                        effective_tolerance,
                    )
                    confirmation_state.reset()
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                if signal.reason.startswith("protective_open_reversal_stop"):
                    jump_reset = False
                    adverse_jump = Decimal("0")
                    jump_threshold = Decimal("0")
                else:
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
                    confirmation_state.reset()
                required_confirmations = (
                    1
                    if signal.reason.startswith("protective_open_reversal_stop")
                    else args.hedge_signal_confirmations
                    if is_protective_hedge
                    else 1
                    if signal.reason.startswith("one_way_trend")
                    else args.late_signal_confirmations
                    if args.strategy == "late_favorite"
                    else primary_signal_confirmation_count(
                        args.strategy,
                        args.signal_confirmations,
                    )
                )
                confirmed, confirmation_status = advance_signal_confirmation(
                    confirmation_state,
                    signal,
                    time.monotonic(),
                    max(1, required_confirmations),
                    (
                        0.0
                        if signal.reason.startswith("protective_open_reversal_stop")
                        else hedge_confirmation_min_seconds
                        if is_protective_hedge
                        else 0.0
                    ),
                    hedge_max_price_worsening if is_protective_hedge else None,
                )
                if not confirmed:
                    logger.info(
                        "SIGNAL_PENDING side=%s %s initial_price=%s current_price=%s",
                        signal.side,
                        confirmation_status,
                        confirmation_state.initial_price,
                        signal.price,
                    )
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                confirmation_state.reset()
                try:
                    latest_price_to_beat = price_to_beat_client.fetch(
                        current_market.event_start_time,
                        current_market.end_time,
                    )
                except Exception as exc:
                    logger.warning(
                        "ORDER_BLOCKED_PRICE_TO_BEAT_UNAVAILABLE slug=%s error=%s",
                        current_market.slug,
                        exc,
                    )
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                if latest_price_to_beat.price_to_beat != start_price:
                    logger.warning(
                        "PRICE_TO_BEAT_CHANGED slug=%s previous=%s latest=%s; "
                        "resetting samples and confirmations",
                        current_market.slug,
                        start_price,
                        latest_price_to_beat.price_to_beat,
                    )
                    start_price = None
                    open_price_tracker.reset()
                    open_price_tracker.observe(
                        latest_price_to_beat.price_to_beat,
                        time.monotonic(),
                    )
                    prices = []
                    price_sample_times = []
                    up_ask_prices = []
                    down_ask_prices = []
                    confirmation_state.reset()
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue

                confirmed_signal = signal
                try:
                    refreshed_spot = price_client.btc_usd()
                    if refreshed_spot.observed_at is not None:
                        refreshed_age = abs(int(time.time()) - refreshed_spot.observed_at)
                        if refreshed_age > args.max_spot_age:
                            raise RuntimeError(
                                f"Pre-submit Chainlink report is stale by {refreshed_age}s"
                            )
                    refreshed_up_book, refreshed_down_book = clob.books(
                        current_market.token_ids
                    )
                    refreshed_up_quote = refreshed_up_book.quote
                    refreshed_down_quote = refreshed_down_book.quote
                except Exception as exc:
                    logger.warning(
                        "ORDER_BLOCKED_PRE_SUBMIT_REFRESH slug=%s error=%s",
                        current_market.slug,
                        exc,
                    )
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue

                refreshed_quote = (
                    refreshed_up_quote if confirmed_signal.side == "UP" else refreshed_down_quote
                )
                if refreshed_quote is None or refreshed_quote.ask is None:
                    logger.info(
                        "ORDER_BLOCKED_PRE_SUBMIT_QUOTE slug=%s side=%s reason=missing_ask",
                        current_market.slug,
                        confirmed_signal.side,
                    )
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                ask_drop = confirmed_signal.price - refreshed_quote.ask
                if ask_drop > pre_submit_max_adverse_ask_drop:
                    logger.info(
                        "ORDER_BLOCKED_ADVERSE_ASK_DROP slug=%s side=%s confirmed_ask=%s "
                        "latest_ask=%s drop=%s max_drop=%s",
                        current_market.slug,
                        confirmed_signal.side,
                        confirmed_signal.price,
                        refreshed_quote.ask,
                        ask_drop,
                        pre_submit_max_adverse_ask_drop,
                    )
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                ask_worsening = refreshed_quote.ask - confirmed_signal.price
                if ask_worsening > pre_submit_max_ask_worsening:
                    logger.info(
                        "ORDER_BLOCKED_ASK_WORSENING slug=%s side=%s confirmed_ask=%s "
                        "latest_ask=%s worsening=%s max_worsening=%s",
                        current_market.slug,
                        confirmed_signal.side,
                        confirmed_signal.price,
                        refreshed_quote.ask,
                        ask_worsening,
                        pre_submit_max_ask_worsening,
                    )
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue

                refreshed_at = time.monotonic()
                submit_not_after_monotonic = (
                    refreshed_at + pre_submit_max_quote_age_seconds
                )
                refreshed_prices = [*prices, refreshed_spot.price]
                refreshed_sample_times = [*price_sample_times, refreshed_at]
                refreshed_seconds_to_end = max(
                    Decimal("0"),
                    _seconds_to_end(current_market, datetime.now(timezone.utc)),
                )
                refreshed_sigma = estimate_sigma_per_sqrt_second(
                    refreshed_prices,
                    Decimal(str(poll_interval)),
                    fallback_sigma,
                    refreshed_sample_times,
                )
                refreshed_fair = btc_up_probability(
                    start_price,
                    refreshed_spot.price,
                    refreshed_seconds_to_end,
                    refreshed_sigma,
                )

                refreshed_signal: AutoTradeSignal | None
                if is_protective_hedge:
                    if confirmed_signal.reason.startswith("protective_open_reversal_stop"):
                        refreshed_required_reversal_seconds = (
                            one_way_reversal_seconds
                            if refreshed_seconds_to_end <= one_way_reversal_final_window_seconds
                            else one_way_reversal_early_seconds
                        )
                        refreshed_reversal_buffer = protective_open_cross_buffer(
                            start_price,
                            refreshed_fair.sigma_per_sqrt_second,
                            Decimal(str(refreshed_required_reversal_seconds)),
                            one_way_reversal_min_usd,
                            hedge_open_cross_sigma_multiplier,
                        )
                        refreshed_signal = choose_open_reversal_stop_signal(
                            current_market,
                            primary_side_this_window or "",
                            refreshed_spot.price,
                            start_price,
                            refreshed_up_quote,
                            refreshed_down_quote,
                            refreshed_seconds_to_end,
                            hedge_entry_cutoff_seconds,
                            refreshed_reversal_buffer,
                            one_way_reversal_min_bid,
                            one_way_reversal_max_entry,
                            hedge_max_spread,
                            min_ask_sum,
                            max_ask_sum,
                        )
                    elif confirmed_signal.reason.startswith("protective_market_reversal"):
                        refreshed_signal = choose_market_reversal_hedge_signal(
                            current_market,
                            primary_side_this_window or "",
                            refreshed_up_quote,
                            refreshed_down_quote,
                            refreshed_seconds_to_end,
                            hedge_entry_start_seconds,
                            hedge_entry_cutoff_seconds,
                            hedge_market_reversal_threshold,
                            hedge_max_entry,
                            hedge_max_spread,
                            min_ask_sum,
                            max_ask_sum,
                        )
                    else:
                        refreshed_signal = choose_protective_hedge_signal(
                            current_market,
                            primary_side_this_window or "",
                            refreshed_fair.probability_up,
                            refreshed_up_quote,
                            refreshed_down_quote,
                            refreshed_seconds_to_end,
                            hedge_entry_start_seconds,
                            hedge_entry_cutoff_seconds,
                            hedge_max_entry,
                            hedge_min_edge,
                            hedge_min_win_probability,
                            hedge_max_spread,
                            min_ask_sum,
                            max_ask_sum,
                        )
                    if not confirmed_signal.reason.startswith("protective_open_reversal_stop"):
                        open_cross_buffer = protective_open_cross_buffer(
                            start_price,
                            refreshed_fair.sigma_per_sqrt_second,
                            Decimal(str(hedge_confirmation_min_seconds)),
                            hedge_open_cross_min_usd,
                            hedge_open_cross_sigma_multiplier,
                        )
                        if (
                            refreshed_signal is not None
                            and not protective_spot_confirms_open_cross(
                                refreshed_prices,
                                start_price,
                                refreshed_signal.side,
                                open_cross_buffer,
                            )
                        ):
                            refreshed_signal = None
                elif args.strategy == "late_favorite":
                    refreshed_signal = choose_late_favorite_signal(
                        current_market,
                        refreshed_fair.probability_up,
                        refreshed_up_quote,
                        refreshed_down_quote,
                        refreshed_seconds_to_end,
                        refreshed_prices,
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
                        refreshed_fair.sigma_per_sqrt_second,
                        late_volatility_buffer_multiplier,
                    )
                elif args.strategy == "late_one_way":
                    refreshed_signal = choose_one_way_trend_signal(
                        current_market,
                        refreshed_up_quote,
                        refreshed_down_quote,
                        refreshed_seconds_to_end,
                        refreshed_spot.price,
                        start_price,
                        [
                            *up_ask_prices,
                            *(
                                [refreshed_up_quote.ask]
                                if refreshed_up_quote is not None
                                and refreshed_up_quote.ask is not None
                                else []
                            ),
                        ],
                        [
                            *down_ask_prices,
                            *(
                                [refreshed_down_quote.ask]
                                if refreshed_down_quote is not None
                                and refreshed_down_quote.ask is not None
                                else []
                            ),
                        ],
                        one_way_entry_seconds,
                        one_way_entry_cutoff_seconds,
                        one_way_min_entry,
                        one_way_max_entry,
                        args.one_way_trend_samples,
                        max_spread,
                        min_ask_sum,
                        max_ask_sum,
                    )
                elif args.strategy == "smart_score":
                    refreshed_signal = choose_smart_score_signal(
                        current_market,
                        refreshed_fair.probability_up,
                        refreshed_up_quote,
                        refreshed_down_quote,
                        refreshed_seconds_to_end,
                        smart_score_entry_seconds,
                        smart_score_cutoff_seconds,
                        min_entry,
                        max_entry,
                        edge_threshold,
                        max_spread,
                        min_ask_sum,
                        max_ask_sum,
                        refreshed_prices,
                        start_price,
                        [
                            *up_ask_prices,
                            *(
                                [refreshed_up_quote.ask]
                                if refreshed_up_quote is not None
                                and refreshed_up_quote.ask is not None
                                else []
                            ),
                        ],
                        [
                            *down_ask_prices,
                            *(
                                [refreshed_down_quote.ask]
                                if refreshed_down_quote is not None
                                and refreshed_down_quote.ask is not None
                                else []
                            ),
                        ],
                        smart_score_threshold,
                        smart_score_min_probability,
                        smart_score_fee_rate,
                        smart_score_slippage,
                        args.smart_score_trend_samples,
                        args.smart_score_stability_samples,
                        probability_shrinkage,
                    )
                else:
                    refreshed_signal = choose_fair_value_edge_signal(
                        current_market,
                        refreshed_fair.probability_up,
                        refreshed_up_quote,
                        refreshed_down_quote,
                        refreshed_seconds_to_end,
                        decision_seconds_before_end,
                        min_entry,
                        max_entry,
                        edge_threshold,
                        max_spread,
                        min_ask_sum,
                        max_ask_sum,
                        min_seconds_before_end,
                        min_win_probability,
                        refreshed_prices,
                        start_price,
                        low_entry_cutoff,
                        low_entry_min_win_probability,
                        args.low_entry_confirmation_samples,
                        probability_shrinkage,
                    )
                    if (
                        refreshed_signal is not None
                        and not recent_spot_samples_support_side(
                            refreshed_prices,
                            start_price,
                            refreshed_signal.side,
                            args.trend_confirmation_samples,
                            (
                                trend_pullback_tolerance_usd
                                if refreshed_signal.price >= low_entry_cutoff
                                else Decimal("0")
                            ),
                            (
                                trend_pullback_tolerance_percent
                                if refreshed_signal.price >= low_entry_cutoff
                                else Decimal("0")
                            ),
                        )
                    ):
                        refreshed_signal = None

                if refreshed_signal is None or refreshed_signal.side != confirmed_signal.side:
                    logger.info(
                        "ORDER_BLOCKED_SIGNAL_CHANGED slug=%s confirmed_side=%s latest_side=%s",
                        current_market.slug,
                        confirmed_signal.side,
                        refreshed_signal.side if refreshed_signal is not None else "NONE",
                    )
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue

                signal = refreshed_signal
                spot = refreshed_spot
                prices = refreshed_prices
                price_sample_times = refreshed_sample_times
                fair = refreshed_fair
                up_quote, down_quote = refreshed_up_quote, refreshed_down_quote
                seconds_to_end = refreshed_seconds_to_end
                trade_size = order_size
                quoted_ask = signal.price
                if trader is not None:
                    maximum_execution_price = (
                        one_way_reversal_max_entry + live_buy_slippage
                        if signal.reason.startswith("protective_open_reversal_stop")
                        else hedge_max_entry + live_buy_slippage
                        if is_protective_hedge
                        else late_max_entry + live_buy_slippage
                        if args.strategy == "late_favorite"
                        else one_way_max_entry
                        if signal.reason.startswith("one_way_trend")
                        else max_entry + live_buy_slippage
                    )
                    if signal.reason.startswith("one_way_trend"):
                        execution_price = buy_limit_price_with_slippage(
                            quoted_ask,
                            live_buy_slippage,
                            current_market.minimum_tick_size,
                            maximum_execution_price,
                        )
                    elif not is_protective_hedge and is_fair_value_strategy(args.strategy):
                        selected_probability = (
                            shrink_probability_toward_even(
                                fair.probability_up,
                                probability_shrinkage,
                            )
                            if signal.side == "UP"
                            else Decimal("1")
                            - shrink_probability_toward_even(
                                fair.probability_up,
                                probability_shrinkage,
                            )
                        )
                        execution_price = buy_limit_price_preserving_edge(
                            quoted_ask,
                            live_buy_slippage,
                            current_market.minimum_tick_size,
                            maximum_execution_price,
                            selected_probability,
                            seconds_to_end,
                            edge_threshold,
                        )
                    elif is_protective_hedge and signal.reason.startswith("protective_hedge"):
                        selected_probability = (
                            fair.probability_up
                            if signal.side == "UP"
                            else Decimal("1") - fair.probability_up
                        )
                        execution_price = buy_limit_price_with_slippage(
                            quoted_ask,
                            live_buy_slippage,
                            current_market.minimum_tick_size,
                            min(maximum_execution_price, selected_probability - hedge_min_edge),
                        )
                    else:
                        execution_price = buy_limit_price_with_slippage(
                            quoted_ask,
                            live_buy_slippage,
                            current_market.minimum_tick_size,
                            maximum_execution_price,
                        )
                    signal = AutoTradeSignal(
                        side=signal.side,
                        token_id=signal.token_id,
                        price=execution_price,
                        reason=(
                            f"{signal.reason} quoted_ask={quoted_ask} "
                            f"max_slippage={live_buy_slippage} "
                            f"applied_slippage={execution_price - quoted_ask}"
                        ),
                    )
                if is_protective_hedge:
                    is_one_way_reversal = (
                        args.strategy == "late_one_way"
                        and signal.reason.startswith("protective_open_reversal_stop")
                    )
                    if is_one_way_reversal:
                        trade_size = order_size
                        protection_notional = signal.price * trade_size
                    else:
                        protection_notional = (
                            min(primary_cost_this_window, hedge_max_live_notional)
                            if hedge_max_live_notional > 0
                            else primary_cost_this_window
                        )
                        trade_size = (
                            protection_notional / signal.price
                        ).quantize(Decimal("0.000001"), rounding=ROUND_FLOOR)
                    hedge_risk = evaluate_protective_hedge_risk(
                        primary_side_this_window or "",
                        primary_cost_this_window,
                        primary_shares_this_window,
                        signal.price,
                        trade_size,
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
                    if is_one_way_reversal:
                        loss_reduction = (
                            hedge_risk.max_loss_before - hedge_risk.max_loss_after
                        )
                        required_loss_reduction = max(
                            primary_cost_this_window
                            * one_way_reversal_min_loss_reduction_percent,
                            one_way_reversal_min_loss_reduction_notional,
                        )
                        if loss_reduction < required_loss_reduction:
                            logger.info(
                                "HEDGE_REJECTED slug=%s side=%s loss_reduction=%s "
                                "required_loss_reduction=%s",
                                current_market.slug,
                                signal.side,
                                loss_reduction.quantize(Decimal("0.0001")),
                                required_loss_reduction.quantize(Decimal("0.0001")),
                            )
                            sleep_until_next_poll(poll_interval, iteration_started_at)
                            continue
                    signal = AutoTradeSignal(
                        side=signal.side,
                        token_id=signal.token_id,
                        price=signal.price,
                        reason=(
                            f"{signal.reason} "
                            f"aggregate_primary_orders={primary_orders_this_window} "
                            f"aggregate_primary_cost={primary_cost_this_window.quantize(Decimal('0.0001'))} "
                            f"aggregate_primary_shares={primary_shares_this_window.quantize(Decimal('0.0001'))} "
                            f"protection_notional={protection_notional.quantize(Decimal('0.0001'))} "
                            f"protected_shares={trade_size.quantize(Decimal('0.0001'))} "
                            f"max_loss_before={hedge_risk.max_loss_before.quantize(Decimal('0.0001'))} "
                            f"max_loss_after={hedge_risk.max_loss_after.quantize(Decimal('0.0001'))} "
                            f"loss_reduction={(hedge_risk.max_loss_before - hedge_risk.max_loss_after).quantize(Decimal('0.0001'))}"
                        ),
                    )
                if trader is not None:
                    selected_book = (
                        refreshed_up_book
                        if signal.side == "UP"
                        else refreshed_down_book
                    )
                    available_depth = executable_ask_depth(
                        selected_book,
                        signal.price,
                    )
                    if available_depth < trade_size:
                        logger.info(
                            "ORDER_BLOCKED_INSUFFICIENT_DEPTH slug=%s side=%s "
                            "limit=%s required=%s available=%s",
                            current_market.slug,
                            signal.side,
                            signal.price,
                            trade_size,
                            available_depth,
                        )
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    signal = AutoTradeSignal(
                        side=signal.side,
                        token_id=signal.token_id,
                        price=signal.price,
                        reason=(
                            f"{signal.reason} "
                            f"pre_submit_depth={available_depth.quantize(Decimal('0.000001'))} "
                            f"quote_ttl={pre_submit_max_quote_age_seconds:g}s"
                        ),
                    )
                logger.info(
                    "AUTO_SIGNAL %s side=%s price=%s size=%s reason=%s",
                    current_market.slug,
                    signal.side,
                    signal.price,
                    trade_size,
                    signal.reason,
                )
                if trader is None:
                    signals_this_window = window_trade_count_after_attempt(
                        signals_this_window,
                        live=False,
                    )
                    if args.paper_trading:
                        paper_trade_stake = (
                            signal.price * paper_shares
                            if paper_shares > 0
                            else paper_stake
                        )
                        paper_fee_rate = (
                            late_fee_rate
                            if args.strategy == "late_favorite"
                            else smart_score_fee_rate
                            if args.strategy == "smart_score"
                            else Decimal("0")
                        )
                        paper_bankroll = open_paper_position(
                            paper_positions,
                            paper_bankroll,
                            current_market.slug,
                            signal,
                            paper_trade_stake,
                            paper_fee_rate,
                        )
                        if args.stop_when_bust and paper_bankroll <= 0:
                            logger.info("PAPER_BUST bankroll=%s. Exiting after open position.", paper_bankroll)
                            return
                    else:
                        logger.info("DRY RUN: would buy %s at %s x %s", signal.side, signal.price, trade_size)
                    if is_protective_hedge:
                        aggregate_protection_completed = True
                        one_way_reversal_started_at = None
                    elif primary_side_this_window is None:
                        primary_side_this_window = signal.side
                        primary_orders_this_window = 1
                        if args.paper_trading:
                            primary_cost_this_window = paper_trade_stake
                            primary_shares_this_window = paper_trade_stake / signal.price
                        else:
                            primary_cost_this_window = signal.price * trade_size
                            primary_shares_this_window = trade_size
                    elif (
                        is_fair_value_strategy(args.strategy)
                        and signal.side == primary_side_this_window
                    ):
                        primary_orders_this_window += 1
                        if args.paper_trading:
                            primary_cost_this_window += paper_trade_stake
                            primary_shares_this_window += paper_trade_stake / signal.price
                        else:
                            primary_cost_this_window += signal.price * trade_size
                            primary_shares_this_window += trade_size
                else:
                    notional = signal.price * trade_size
                    live_notional_cap = (
                        protection_notional
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
                        "size": str(trade_size),
                        "notional": str(notional),
                        "order_type": args.live_order_type,
                        "order_role": (
                            "reverse_protection"
                            if is_protective_hedge
                            else "primary"
                            if primary_side_this_window is None
                            else "same_direction_add"
                        ),
                        "quoted_ask": str(quoted_ask),
                        "max_slippage": str(live_buy_slippage),
                        "applied_slippage": str(signal.price - quoted_ask),
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
                            size=trade_size,
                            tick_size=current_market.minimum_tick_size,
                            neg_risk=current_market.neg_risk,
                            order_type=args.live_order_type,
                            submit_not_after_monotonic=submit_not_after_monotonic,
                        )
                    except OrderQuoteExpiredError as exc:
                        live_orders_submitted -= 1
                        live_summary["status"] = "running"
                        live_summary["order_attempts"] = live_orders_submitted
                        live_summary["error"] = None
                        order_record["error"] = f"{type(exc).__name__}: {exc}"
                        write_live_summary(finalize=False)
                        logger.info(
                            "ORDER_BLOCKED_QUOTE_EXPIRED slug=%s side=%s error=%s",
                            current_market.slug,
                            signal.side,
                            exc,
                        )
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
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
                    matched = live_response_is_matched(
                        response,
                        require_fill_amounts=args.live_order_type == "FAK",
                    )
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
                    if is_protective_hedge:
                        aggregate_protection_completed = True
                        one_way_reversal_started_at = None
                    elif primary_side_this_window is None:
                        primary_side_this_window = signal.side
                        primary_orders_this_window = 1
                        primary_cost_this_window, primary_shares_this_window = response_fill_amounts(
                            response,
                            signal.price,
                            trade_size,
                        )
                    elif (
                        is_fair_value_strategy(args.strategy)
                        and signal.side == primary_side_this_window
                    ):
                        fill_cost, fill_shares = response_fill_amounts(
                            response,
                            signal.price,
                            trade_size,
                        )
                        primary_orders_this_window += 1
                        primary_cost_this_window += fill_cost
                        primary_shares_this_window += fill_shares
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
                confirmation_state.reset()

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
