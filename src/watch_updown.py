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
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from requests import RequestException

from src import __version__
from src.crypto_resolution import (
    CryptoResolutionMode,
    detect_crypto_resolution_mode,
    has_official_btc_5m_twap_rule,
)
from src.fair_value import (
    btc_up_probability,
    btc_up_twap_probability,
    choose_theoretical_action,
    estimate_sigma_per_sqrt_second,
    trailing_time_weighted_average,
)
from src.fast_directional_hedge_simple import (
    FastDirectionalHedgeSimpleEngine,
    FastDirectionalHedgeSimpleSettings,
)
from src.ewma_twap_fair import (
    EwmaTwapSettings,
    choose_ewma_twap_decision,
    ewma_twap_fair_value,
)
from src.market_recorder import JsonlSnapshotWriter, build_snapshot
from src.manual_trading import ManualTradeExecutor
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
from src.polygon_resolution import PolygonResolutionReader
from src.reversal_runtime import (
    ChainResultMismatch,
    GammaResultMismatch,
    ReversalRuntime,
    market_health_from_books,
    previous_5m_slug,
    reversal_startup_self_check,
)
from src.reversal_v11 import (
    FIRST_STAGE_ONLY_STAKES,
    SPARSE_RECOVERY_NOTIONALS,
    TWO_WINDOW_FIXED_NOTIONALS,
    Direction,
    MarketHealth,
    ReversalV11,
)
from src.telegram_commands import (
    DEFAULT_LAUNCH_STRATEGY,
    LIVE_STRATEGIES,
    REVERSAL_STRATEGIES,
    REVERSAL_TRIGGER_STREAKS,
    STRATEGY_LABELS,
)
from src.telegram_notify import TradingNotificationService


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("btc-updown-watch")

REVERSAL_COMPLETED_FAST_ATTEMPTS = 1


SLUG_PATTERN = re.compile(r"^(btc-updown-5m-)(\d+)$")
GAMMA_API = "https://gamma-api.polymarket.com"
REVERSAL_FAST_FAK_RETRY_DELAY_SECONDS = 0.15
_ACTIVE_NOTIFICATIONS: TradingNotificationService | None = None


def is_reversal_clob_timeout(exc: BaseException) -> bool:
    """Return true for transient CLOB transport timeouts handled fail-closed."""
    error = str(exc).lower()
    return isinstance(exc, requests.exceptions.Timeout) and (
        "clob.polymarket.com" in error or "clob api" in error
    )


def reversal_profile_overrides(strategy: str) -> dict[str, object]:
    overrides: dict[str, object] = {
        "trigger_streak": REVERSAL_TRIGGER_STREAKS.get(strategy, 2),
        "first_stage_rv60_filter_enabled": True,
        "first_stage_rv300_filter_enabled": True,
    }
    if strategy in {"reversal_v11", "reversal_v11_four_streak"}:
        overrides["maximum_attempts"] = 10
        overrides["first_attempt_uses_first_stage_rules"] = True
        overrides["full_loss_recovery_start_attempt"] = 2
        overrides["full_loss_recovery_strict_funding"] = True
        overrides["continue_final_stage_until_success_or_unfunded"] = True
        overrides["market_filters_enabled"] = False
    if strategy == "reversal_four_64":
        overrides.update(
            allocated_capital=Decimal("64"),
            maximum_streak_loss=Decimal("64"),
            hard_round_loss_limit=None,
            soft_round_loss_limit=Decimal("64"),
            one_final_recovery_after_soft_limit=False,
            end_round_at_soft_loss_limit=True,
            full_loss_recovery_start_attempt=2,
            full_loss_recovery_strict_funding=True,
            continue_final_stage_until_success_or_unfunded=False,
        )
    elif strategy == "reversal_three_16":
        overrides.update(
            allocated_capital=Decimal("16"),
            maximum_streak_loss=Decimal("16"),
            hard_round_loss_limit=None,
            soft_round_loss_limit=Decimal("16"),
            one_final_recovery_after_soft_limit=False,
            end_round_at_soft_loss_limit=True,
            full_loss_recovery_start_attempt=2,
            full_loss_recovery_strict_funding=True,
            continue_final_stage_until_success_or_unfunded=False,
        )
    elif strategy in {"reversal_first_stage", "reversal_or_fair_value"}:
        overrides.update(
            first_stage_only_enabled=True,
            stakes=FIRST_STAGE_ONLY_STAKES,
            full_loss_recovery_enabled=False,
            dynamic_final_recovery_enabled=False,
            market_filters_enabled=False,
        )
    elif strategy == "reversal_three_4_8":
        overrides.update(
            stakes=TWO_WINDOW_FIXED_NOTIONALS,
            fixed_notional_stages=TWO_WINDOW_FIXED_NOTIONALS,
            fixed_notional_max_ask=Decimal("0.49"),
            fixed_notional_recovery_loss_start_attempt=6,
            fixed_notional_recovery_start_attempt=7,
            continue_final_stage_until_success_or_unfunded=True,
            full_loss_recovery_enabled=False,
            dynamic_final_recovery_enabled=False,
            market_filters_enabled=False,
        )
    elif strategy == "reversal_v11_six_streak":
        overrides.update(
            stakes=SPARSE_RECOVERY_NOTIONALS,
            sparse_recovery_notional_stages=SPARSE_RECOVERY_NOTIONALS,
            sparse_recovery_start_attempt=5,
            sparse_recovery_loss_start_attempt=4,
            continue_final_stage_until_success_or_unfunded=True,
            maximum_attempts=10,
            full_loss_recovery_enabled=False,
            dynamic_final_recovery_enabled=False,
            market_filters_enabled=False,
        )
    return overrides


@dataclass(frozen=True)
class AutoTradeSignal:
    side: str
    token_id: str
    price: Decimal
    reason: str
    size: Decimal | None = None
    fair_probability: Decimal | None = None
    action: str = "BUY"
    role: str = "ENTRY"
    executable_price: Decimal | None = None


@dataclass(frozen=True)
class BookTrendEvidence:
    side: str
    selected_slope: Decimal
    opposite_slope: Decimal
    relative_slope: Decimal
    pullback: Decimal
    span_seconds: Decimal
    samples: int


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
    winner: str | None = None
    exit_shares: Decimal = Decimal("0")
    exit_proceeds: Decimal = Decimal("0")
    exit_fee: Decimal = Decimal("0")


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


class AmbiguousTwapResult(RuntimeError):
    def __init__(
        self,
        slug: str,
        price_to_beat: Decimal,
        ending_twap: Decimal,
    ) -> None:
        self.slug = slug
        self.price_to_beat = price_to_beat
        self.ending_twap = ending_twap
        self.move = ending_twap - price_to_beat
        super().__init__(
            f"ambiguous Chainlink TWAP result for {slug}: "
            f"price_to_beat={price_to_beat} ending_twap={ending_twap} "
            f"move={self.move}; wait for terminal book or Gamma final outcome"
        )


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
    parser.add_argument("--ewma-twap-lambda", default="0.94")
    parser.add_argument("--ewma-twap-realized-seconds", default="60")
    parser.add_argument("--ewma-twap-weight", default="0.70")
    parser.add_argument("--ewma-twap-min-edge", default="0.015")
    parser.add_argument("--ewma-twap-half-spread-buffer", default="0.0025")
    parser.add_argument("--ewma-twap-slippage-buffer", default="0.0030")
    parser.add_argument("--ewma-twap-fee-rate", default="0.07")
    parser.add_argument("--ewma-twap-kelly-fraction", default="0.25")
    parser.add_argument("--ewma-twap-kelly-bankroll", default="1000")
    parser.add_argument("--ewma-twap-max-notional", default="25")
    parser.add_argument("--ewma-twap-entry-seconds", default="300")
    parser.add_argument("--ewma-twap-cutoff-seconds", default="75")
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
        "--crypto-resolution-mode",
        choices=[mode.value for mode in CryptoResolutionMode],
        default="auto",
        help="Resolution source: auto from market rules, legacy boundary price, or Chainlink 60-second TWAP.",
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
    parser.add_argument(
        "--fair-value-fee-rate",
        default="0.07",
        help="Taker fee coefficient deducted before testing fair-value edge.",
    )
    parser.add_argument(
        "--fair-value-confirmation-min-seconds",
        type=float,
        default=0.0,
        help="Extra duration after the book-trend confirmation; zero avoids duplicating it.",
    )
    parser.add_argument("--fair-value-book-trend-samples", type=int, default=3)
    parser.add_argument("--fair-value-book-min-slope", default="0.003")
    parser.add_argument("--fair-value-book-min-relative-slope", default="0.005")
    parser.add_argument("--fair-value-book-max-pullback", default="0.01")
    parser.add_argument("--fdh-entry-price-min", default="0.53")
    parser.add_argument("--fdh-entry-price-max", default="0.60")
    parser.add_argument("--fdh-entry-confirm-ticks", type=int, default=2)
    parser.add_argument("--fdh-entry-confirm-min-interval-ms", type=int, default=150)
    parser.add_argument("--fdh-base-position-size", default="2")
    parser.add_argument("--fdh-min-ask-gap", default="0.04")
    parser.add_argument("--fdh-max-spread", default="0.04")
    parser.add_argument("--fdh-max-ask-sum", default="1.06")
    parser.add_argument("--fdh-max-entry-drift", default="0.02")
    parser.add_argument("--fdh-entry-max-slippage", default="0.02")
    parser.add_argument("--fdh-initial-stop-pct", default="0.25")
    parser.add_argument("--fdh-trailing-start-gain", default="0.15")
    parser.add_argument("--fdh-break-even-buffer", default="0.00")
    parser.add_argument("--fdh-trailing-drawdown-pct", default="0.20")
    parser.add_argument("--fdh-stop-confirm-ticks", type=int, default=2)
    parser.add_argument("--fdh-fast-move-window-ms", type=int, default=500)
    parser.add_argument("--fdh-fast-move-threshold", default="0.05")
    parser.add_argument("--fdh-fast-stop-confirm-ticks", type=int, default=1)
    parser.add_argument("--fdh-emergency-stop-penetration", default="0.06")
    parser.add_argument("--fdh-hedge-max-slippage", default="0.05")
    parser.add_argument("--fdh-hedge-max-price", default="0.85")
    parser.add_argument("--fdh-hedge-entry-max-seconds", default="150")
    parser.add_argument("--fdh-hedge-entry-min-seconds", default="30")
    parser.add_argument("--fdh-exit-max-slippage", default="0.03")
    parser.add_argument("--fdh-take-profit-net-per-share", default="0.02")
    parser.add_argument("--fdh-take-profit-confirm-ticks", type=int, default=1)
    parser.add_argument("--fdh-max-entries-per-window", type=int, default=2)
    parser.add_argument("--fdh-normal-entry-max-seconds", default="180")
    parser.add_argument("--fdh-normal-entry-min-seconds", default="60")
    parser.add_argument("--fdh-stop-new-entry-time", default="30")
    parser.add_argument("--fdh-risk-only-time", default="15")
    parser.add_argument("--fdh-fee-rate", default="0.07")
    parser.add_argument("--fdh-max-book-age-seconds", default="0.50")
    parser.add_argument(
        "--fdh-state-json",
        default="data/fast_directional_hedge_simple_state.json",
    )
    parser.add_argument(
        "--fdh-record-jsonl",
        default="data/fast_directional_hedge_simple_events.jsonl",
    )
    parser.add_argument("--smart-score-threshold", default="70")
    parser.add_argument("--smart-score-entry-seconds", type=int, default=100)
    parser.add_argument("--smart-score-cutoff-seconds", type=int, default=25)
    parser.add_argument("--smart-score-min-probability", default="0.52")
    parser.add_argument("--smart-score-fee-rate", default="0.07")
    parser.add_argument("--smart-score-slippage", default="0.01")
    parser.add_argument("--smart-score-trend-samples", type=int, default=3)
    parser.add_argument("--smart-score-stability-samples", type=int, default=3)
    parser.add_argument("--momentum-entry-seconds", type=int, default=270)
    parser.add_argument("--momentum-cutoff-seconds", type=int, default=25)
    parser.add_argument("--momentum-min-move-percent", default="0.0004")
    parser.add_argument("--momentum-min-move-usd", default="20")
    parser.add_argument("--momentum-confirmation-seconds", type=int, default=30)
    parser.add_argument("--momentum-min-entry", default="0.45")
    parser.add_argument("--momentum-max-entry", default="0.75")
    parser.add_argument("--momentum-fee-rate", default="0.07")
    parser.add_argument("--fair-scratch-entry-seconds", type=int, default=180)
    parser.add_argument("--fair-scratch-cutoff-seconds", type=int, default=25)
    parser.add_argument("--fair-scratch-min-entry", default="0.45")
    parser.add_argument("--fair-scratch-max-entry", default="0.55")
    parser.add_argument("--fair-scratch-min-probability", default="0.53")
    parser.add_argument("--fair-scratch-min-net-edge", default="0.02")
    parser.add_argument("--fair-scratch-fee-rate", default="0.07")
    parser.add_argument("--fair-scratch-exit-probability", default="0.52")
    parser.add_argument("--fair-scratch-price-tolerance", default="0.02")
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
    parser.add_argument(
        "--reversal-first-stage-max-rv60",
        default=None,
        help=(
            "When set, block only a new reversal round's first stage when BTC "
            "60-second realized volatility is at or above this decimal threshold."
        ),
    )
    parser.add_argument(
        "--reversal-first-stage-max-rv300",
        default=None,
        help=(
            "When set, block only a new reversal round's first stage when BTC "
            "five-minute realized volatility is at or above this decimal threshold."
        ),
    )
    parser.add_argument(
        "--reversal-first-stage-rv300-persistence-ratio",
        default="0.35",
        help=(
            "When RV300 exceeds its base threshold, block only if RV60/RV300 "
            "is at least this ratio, unless the hard multiplier is reached."
        ),
    )
    parser.add_argument(
        "--reversal-first-stage-rv300-hard-multiplier",
        default="2.0",
        help="Always block when RV300 reaches this multiple of its base threshold.",
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


def current_5m_slug(slug: str, now: datetime | None = None) -> str:
    """Return the current wall-clock window while preserving the slug prefix."""
    match = SLUG_PATTERN.match(slug)
    if not match:
        raise ValueError(f"Cannot derive current 5m slug from: {slug}")
    observed_at = now or datetime.now(timezone.utc)
    epoch = int(observed_at.timestamp()) // 300 * 300
    return f"{match.group(1)}{epoch}"


def argv_with_current_slug(argv: list[str], slug: str) -> list[str]:
    """Replace the original --slug so an exec restart never replays stale windows."""
    updated = list(argv)
    for index, value in enumerate(updated):
        if value == "--slug" and index + 1 < len(updated):
            updated[index + 1] = slug
            return updated
        if value.startswith("--slug="):
            updated[index] = f"--slug={slug}"
            return updated
    return ["--slug", slug, *updated]


def weekly_restart_report_day(report_day: date) -> bool:
    """Schedule the weekly restart after Sunday's UTC report (Monday 08:00 CST)."""
    return report_day.weekday() == 6


def weekly_restart_is_safe(state: Any) -> bool:
    """Never restart after stage one has begun until the entire round is over."""
    return state.active_round is None and state.prepared_split is None


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


def fetch_near_certain_market_winner(
    slug: str,
    *,
    winner_min_price: Decimal = Decimal("0.98"),
    loser_max_price: Decimal = Decimal("0.02"),
) -> Direction | None:
    """Read Gamma's current market prices without treating them as final audit."""
    response = requests.get(
        f"{GAMMA_API}/events",
        params={"slug": slug, "limit": 1},
        timeout=3,
    )
    response.raise_for_status()
    events = response.json()
    markets = events[0].get("markets") if events else None
    if not markets:
        return None
    try:
        prices = json.loads(markets[0].get("outcomePrices") or "")
        up_price, down_price = (Decimal(str(prices[0])), Decimal(str(prices[1])))
    except (IndexError, json.JSONDecodeError, TypeError, ValueError):
        return None
    up_wins = up_price >= winner_min_price and down_price <= loser_max_price
    down_wins = down_price >= winner_min_price and up_price <= loser_max_price
    if up_wins == down_wins:
        return None
    return Direction.UP if up_wins else Direction.DOWN


def fetch_reversal_chainlink_open_prices(
    client: PolymarketPriceToBeatClient,
    market: Market,
    current_open_price: Decimal,
    known_prices: dict[str, Decimal] | None = None,
    lookback_windows: int = 2,
) -> dict[str, Decimal]:
    """Load consecutive Price-to-Beat boundaries for the configured result streak."""
    known = known_prices or {}
    prices: dict[str, Decimal] = {}
    for offset in range(lookback_windows, 0, -1):
        slug = previous_5m_slug(market.slug, offset)
        if slug in known:
            prices[slug] = known[slug]
            continue
        start_time = market.event_start_time - timedelta(seconds=300 * offset)
        prices[slug] = client.fetch(
            start_time,
            start_time + timedelta(seconds=300),
        ).open_price
    prices[market.slug] = current_open_price
    return prices


def fetch_reversal_twap_completed_prices(
    market: Market,
    known_price_to_beat: dict[str, Decimal],
    ending_twap: Decimal,
    minimum_decisive_move_usd: Decimal = Decimal("1.00"),
) -> dict[str, tuple[Decimal, Decimal]]:
    """Build one official-rule result from fixed Price to Beat and ending TWAP.

    The Price to Beat is the Chainlink TWAP-60 value fixed at the window's
    opening boundary. The value observed at the next boundary is that window's
    ending TWAP. Legacy instant ``openPrice`` values are a different series and
    must never be substituted. Very small boundary moves fail closed because
    one-second delivery alignment can decide those windows; Gamma's finalized
    outcome subsequently fills them in.
    """
    completed_slug = previous_5m_slug(market.slug)
    price_to_beat = known_price_to_beat.get(completed_slug)
    if price_to_beat is None:
        raise RuntimeError(
            f"fixed Chainlink TWAP Price to Beat unavailable for completed window "
            f"{completed_slug}"
        )
    if ending_twap <= 0:
        raise RuntimeError(
            f"invalid ending Chainlink TWAP for {market.slug}: {ending_twap}"
        )
    if minimum_decisive_move_usd < 0:
        raise ValueError("minimum decisive TWAP move must not be negative")
    move = ending_twap - price_to_beat
    if abs(move) <= minimum_decisive_move_usd:
        raise AmbiguousTwapResult(
            completed_slug,
            price_to_beat,
            ending_twap,
        )
    return {completed_slug: (price_to_beat, ending_twap)}


def terminal_book_winner(
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    *,
    winner_min_bid: Decimal = Decimal("0.98"),
    loser_max_bid: Decimal = Decimal("0.02"),
) -> Direction | None:
    """Return a terminal consensus only from executable best bids."""
    if up_quote is None or down_quote is None:
        return None
    up_wins = up_quote.bid >= winner_min_bid and down_quote.bid <= loser_max_bid
    down_wins = down_quote.bid >= winner_min_bid and up_quote.bid <= loser_max_bid
    if up_wins == down_wins:
        return None
    return Direction.UP if up_wins else Direction.DOWN


def fetch_reversal_completed_window_prices(
    client: PolymarketPriceToBeatClient,
    market: Market,
    last_settled_slug: str | None,
    lookback_windows: int = 2,
) -> dict[str, tuple[Decimal, Decimal]]:
    """Fetch only missing predecessor windows with finalized open/close prices."""
    last_settled_epoch = (
        int(last_settled_slug.rpartition("-")[2]) if last_settled_slug else None
    )
    completed: dict[str, tuple[Decimal, Decimal]] = {}
    for offset in range(lookback_windows, 0, -1):
        slug = previous_5m_slug(market.slug, offset)
        epoch = int(slug.rpartition("-")[2])
        if last_settled_epoch is not None and epoch <= last_settled_epoch:
            continue
        start_time = market.event_start_time - timedelta(seconds=300 * offset)
        result = client.fetch(start_time, start_time + timedelta(seconds=300))
        if not result.completed or result.incomplete or result.close_price is None:
            raise RuntimeError(
                f"completed price pending for {slug}: completed={result.completed} "
                f"incomplete={result.incomplete} close={result.close_price}"
            )
        completed[slug] = (result.open_price, result.close_price)
    return completed


def is_http_rate_limit(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 429


def suppress_reversal_boundary_alert(exc: BaseException) -> bool:
    """Keep expected, retryable boundary states out of realtime alerts."""
    return is_http_rate_limit(exc) or isinstance(exc, AmbiguousTwapResult)


def accept_open_price(
    strategy: str,
    tracker: StableOpenPriceTracker,
    price: Decimal,
    observed_at: float,
) -> Decimal | None:
    """Use the first valid Price to Beat for the latency-sensitive reversal path."""
    if price <= 0:
        return None
    if strategy in REVERSAL_STRATEGIES:
        return price
    return tracker.observe(price, observed_at)


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


def executable_bid_depth(
    book: OrderBookSnapshot,
    minimum_price: Decimal,
) -> Decimal:
    if minimum_price <= 0:
        return Decimal("0")
    return sum(
        (
            level.size
            for level in book.bids
            if level.price >= minimum_price and level.size > 0
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
        if not all(distance > 0 for distance in distances):
            return False
        peak_distance = max(distances)
        tolerance = effective_pullback_tolerance(
            pullback_tolerance_usd,
            peak_distance,
            pullback_tolerance_percent,
        )
        return peak_distance - distances[-1] <= tolerance
    if side == "DOWN":
        distances = [start_price - price for price in recent]
        if not all(distance > 0 for distance in distances):
            return False
        peak_distance = max(distances)
        tolerance = effective_pullback_tolerance(
            pullback_tolerance_usd,
            peak_distance,
            pullback_tolerance_percent,
        )
        return peak_distance - distances[-1] <= tolerance
    return False


def book_trend_evidence(
    up_ask_prices: list[Decimal],
    down_ask_prices: list[Decimal],
    sample_times: list[float],
    side: str,
    sample_count: int = 3,
    minimum_selected_slope: Decimal = Decimal("0.003"),
    minimum_relative_slope: Decimal = Decimal("0.005"),
    maximum_pullback: Decimal = Decimal("0.01"),
) -> BookTrendEvidence | None:
    """Confirm same-window direction from executable asks, not BTC movement.

    The selected token must rise, outperform the opposite token, and remain
    near its recent high. Samples are reset at every five-minute boundary, so
    cross-window prices can never leak into this direction decision.
    """
    if (
        side not in {"UP", "DOWN"}
        or sample_count < 3
        or len(up_ask_prices) < sample_count
        or len(down_ask_prices) < sample_count
        or len(sample_times) < sample_count
        or min(minimum_selected_slope, minimum_relative_slope, maximum_pullback) < 0
    ):
        return None
    recent_up = up_ask_prices[-sample_count:]
    recent_down = down_ask_prices[-sample_count:]
    recent_times = sample_times[-sample_count:]
    if any(current <= previous for previous, current in zip(recent_times, recent_times[1:])):
        return None
    selected = recent_up if side == "UP" else recent_down
    opposite = recent_down if side == "UP" else recent_up
    x = [Decimal(str(value - recent_times[0])) for value in recent_times]
    x_mean = sum(x, Decimal("0")) / Decimal(sample_count)
    denominator = sum(((value - x_mean) ** 2 for value in x), Decimal("0"))
    if denominator <= 0:
        return None

    def slope(values: list[Decimal]) -> Decimal:
        y_mean = sum(values, Decimal("0")) / Decimal(sample_count)
        return (
            sum(
                (
                    (time_value - x_mean) * (price_value - y_mean)
                    for time_value, price_value in zip(x, values)
                ),
                Decimal("0"),
            )
            / denominator
        )

    selected_slope = slope(selected)
    opposite_slope = slope(opposite)
    relative_slope = selected_slope - opposite_slope
    pullback = max(selected) - selected[-1]
    if (
        selected_slope < minimum_selected_slope
        or relative_slope < minimum_relative_slope
        or pullback > maximum_pullback
    ):
        return None
    return BookTrendEvidence(
        side=side,
        selected_slope=selected_slope,
        opposite_slope=opposite_slope,
        relative_slope=relative_slope,
        pullback=pullback,
        span_seconds=x[-1],
        samples=sample_count,
    )


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


def cross_window_volatility_series(
    samples: list[tuple[float, Decimal]] | deque[tuple[float, Decimal]],
    latest: tuple[float, Decimal] | None = None,
) -> tuple[list[Decimal], list[float]]:
    """Return rolling BTC history reserved exclusively for volatility."""
    combined = list(samples)
    if latest is not None and (not combined or latest[0] > combined[-1][0]):
        combined.append(latest)
    return [price for _, price in combined], [observed_at for observed_at, _ in combined]


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
    if strategy == "fast_directional_hedge_simple":
        return max(configured_limit, 8)
    if strategy == "late_one_way":
        return min(2, configured_limit)
    if strategy in {
        "smart_score",
        "ewma_twap_fair",
        "momentum_confirmation",
        "reversal_four_64",
        "open_060",
    }:
        return min(1, configured_limit)
    if is_fair_value_strategy(strategy):
        # --max-trades remains the primary-entry allowance. Aggregate protection
        # gets one additional reserved matched-order slot.
        return configured_limit + 1
    return configured_limit


def is_fair_value_strategy(strategy: str) -> bool:
    return strategy in {
        "fair_value_edge",
        "late_070",
        "reversal_or_fair_value",
    }


def hybrid_fair_value_fallback_allowed(
    strategy: str,
    reversal_order: dict[str, Any] | None,
    active_round: object | None,
    prepared_split: object | None,
    *,
    current_slug: str | None = None,
    signal_owner: str | None = None,
) -> bool:
    """Run the hybrid fair-value channel when reversal owns no current trade.

    Reversal history and fair-value probability are independent signal inputs.
    A stale round from another window must not suppress the fair-value channel,
    while a matched fair-value entry permanently owns the current window so a
    late reversal-history repair cannot create a second strategy entry.
    """
    if strategy != "reversal_or_fair_value" or signal_owner == "reversal":
        return False
    if signal_owner == "fair_value":
        return True
    if reversal_order is not None:
        return False
    if current_slug is None:
        return active_round is None and prepared_split is None
    active_window = getattr(active_round, "awaiting_window", None)
    prepared_window = getattr(prepared_split, "window_slug", None)
    return active_window != current_slug and prepared_window != current_slug


def primary_signal_confirmation_count(strategy: str, configured_count: int) -> int:
    """Return the confirmation count for non-protective primary signals."""
    if strategy in {
        "fair_value_edge",
        "reversal_or_fair_value",
        "late_070",
        "fast_directional_hedge_simple",
        "ewma_twap_fair",
    }:
        return 1
    return configured_count


def choose_ewma_twap_signal(
    market: Market,
    volatility_samples: list[tuple[float, Decimal]],
    underlying_spot: Decimal,
    price_to_beat: Decimal,
    seconds_to_expiry: Decimal,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    settings: EwmaTwapSettings,
    fallback_sigma: Decimal,
) -> AutoTradeSignal | None:
    fair = ewma_twap_fair_value(
        volatility_samples,
        underlying_spot,
        price_to_beat,
        seconds_to_expiry,
        settings,
        fallback_sigma,
    )
    if fair is None:
        return None
    decision = choose_ewma_twap_decision(
        fair,
        up_quote.ask if up_quote is not None else None,
        down_quote.ask if down_quote is not None else None,
        seconds_to_expiry,
        settings,
    )
    if decision is None:
        return None
    token_index = 0 if decision.side == "UP" else 1
    return AutoTradeSignal(
        side=decision.side,
        token_id=market.token_ids[token_index],
        price=decision.price,
        size=decision.shares,
        fair_probability=decision.probability,
        reason=(
            f"ewma_twap_fair p={decision.probability:.4f} "
            f"raw_edge={decision.raw_edge:.4f} fee={decision.fee_per_share:.4f} "
            f"net_model_edge={decision.net_model_edge:.4f} "
            f"sigma={fair.sigma_per_sqrt_second:.8f} "
            f"effective_seconds={fair.effective_variance_seconds:.2f} "
            f"kelly_notional={decision.notional:.4f}"
        ),
    )


def choose_fast_directional_hedge_simple_signal(
    engine: FastDirectionalHedgeSimpleEngine,
    market: Market,
    start_price: Decimal,
    spot_price: Decimal,
    seconds_to_end: Decimal,
    sigma_per_sqrt_second: Decimal,
    base_probability_up: Decimal,
    prices: list[Decimal],
    sample_times: list[float],
    up_book: OrderBookSnapshot,
    down_book: OrderBookSnapshot,
    observed_at: float | None = None,
) -> AutoTradeSignal | None:
    decision = engine.evaluate(
        slug=market.slug,
        seconds_to_expiry=seconds_to_end,
        up_book=up_book,
        down_book=down_book,
        spot_price=spot_price,
        price_to_beat=start_price,
        sigma_per_sqrt_second=sigma_per_sqrt_second,
        observed_at=observed_at,
    )
    if decision is None:
        return None
    token_id = market.token_ids[0] if decision.side == "UP" else market.token_ids[1]
    return AutoTradeSignal(
        side=decision.side,
        token_id=token_id,
        price=decision.limit_price,
        reason=decision.reason,
        size=decision.quantity,
        fair_probability=decision.probability,
        action=decision.action,
        role=decision.role,
        executable_price=decision.executable_price,
    )


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
    fee_rate: Decimal = Decimal("0"),
) -> Decimal:
    tick = Decimal(tick_size)
    candidate = buy_limit_price_with_slippage(
        ask_price,
        slippage,
        tick_size,
        maximum_price,
    )
    while candidate > ask_price:
        fee_per_share = fee_rate * candidate * (Decimal("1") - candidate)
        if probability - candidate - fee_per_share >= required_fair_value_edge(
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
    up_ask_prices: list[Decimal] | None = None,
    down_ask_prices: list[Decimal] | None = None,
    book_sample_times: list[float] | None = None,
    book_trend_samples: int = 3,
    book_trend_min_slope: Decimal = Decimal("0.003"),
    book_trend_min_relative_slope: Decimal = Decimal("0.005"),
    book_trend_max_pullback: Decimal = Decimal("0.01"),
    low_entry_cutoff: Decimal = Decimal("0.50"),
    low_entry_min_win_probability: Decimal = Decimal("0.61"),
    probability_shrinkage: Decimal = Decimal("1"),
    fee_rate: Decimal = Decimal("0"),
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
    up_raw_edge = probability_up - up_quote.ask
    down_raw_edge = down_probability - down_quote.ask
    up_fee = fee_rate * up_quote.ask * (Decimal("1") - up_quote.ask)
    down_fee = fee_rate * down_quote.ask * (Decimal("1") - down_quote.ask)
    up_edge = up_raw_edge - up_fee
    down_edge = down_raw_edge - down_fee
    if (
        not up_ask_prices
        or not down_ask_prices
        or up_ask_prices[-1] != up_quote.ask
        or down_ask_prices[-1] != down_quote.ask
    ):
        return None
    trend_candidates = [
        evidence
        for evidence in (
            book_trend_evidence(
                up_ask_prices,
                down_ask_prices,
                book_sample_times or [],
                "UP",
                book_trend_samples,
                book_trend_min_slope,
                book_trend_min_relative_slope,
                book_trend_max_pullback,
            ),
            book_trend_evidence(
                up_ask_prices,
                down_ask_prices,
                book_sample_times or [],
                "DOWN",
                book_trend_samples,
                book_trend_min_slope,
                book_trend_min_relative_slope,
                book_trend_max_pullback,
            ),
        )
        if evidence is not None
    ]
    if not trend_candidates:
        return None
    trend = max(
        trend_candidates,
        key=lambda evidence: (evidence.relative_slope, evidence.selected_slope),
    )
    if trend.side == "UP":
        side = "UP"
        token_id = market.token_ids[0]
        entry = up_quote.ask
        edge = up_edge
        raw_edge = up_raw_edge
        fee_per_share = up_fee
    else:
        side = "DOWN"
        token_id = market.token_ids[1]
        entry = down_quote.ask
        edge = down_edge
        raw_edge = down_raw_edge
        fee_per_share = down_fee

    if entry < min_entry or entry > max_entry:
        return None
    selected_probability = probability_up if side == "UP" else down_probability
    required_probability = min_win_probability
    if entry < low_entry_cutoff:
        required_probability = max(required_probability, low_entry_min_win_probability)
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
            f"fair_value_edge entry={entry} raw_edge={raw_edge.quantize(Decimal('0.0001'))} "
            f"fee={fee_per_share.quantize(Decimal('0.0001'))} "
            f"net_edge={edge.quantize(Decimal('0.0001'))} "
            f"required_edge={required_edge.quantize(Decimal('0.0001'))} "
            f"required_probability={required_probability.quantize(Decimal('0.0001'))} "
            f"p_up={probability_up.quantize(Decimal('0.0001'))} "
            f"raw_p_up={raw_probability_up.quantize(Decimal('0.0001'))} "
            f"shrinkage={probability_shrinkage.quantize(Decimal('0.01'))} "
            f"book_samples={trend.samples} "
            f"book_slope={trend.selected_slope:+.6f}/s "
            f"opposite_slope={trend.opposite_slope:+.6f}/s "
            f"relative_slope={trend.relative_slope:+.6f}/s "
            f"book_span={trend.span_seconds:.3f}s "
            f"book_pullback={trend.pullback:.4f} "
            f"seconds_left={int(seconds_to_end)}"
        ),
    )


def choose_momentum_confirmation_signal(
    market: Market,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    recent_spot_prices: list[Decimal],
    recent_sample_times: list[float],
    start_price: Decimal,
    entry_seconds: Decimal = Decimal("240"),
    cutoff_seconds: Decimal = Decimal("25"),
    min_move_percent: Decimal = Decimal("0.0004"),
    min_move_usd: Decimal = Decimal("40"),
    min_entry: Decimal = Decimal("0.45"),
    max_entry: Decimal = Decimal("0.70"),
    max_spread: Decimal = Decimal("0.05"),
    min_ask_sum: Decimal = Decimal("0.90"),
    max_ask_sum: Decimal = Decimal("1.10"),
    confirmation_seconds: int = 30,
) -> AutoTradeSignal | None:
    """Strategy A: direction comes only from BTC momentum and order-book skew."""
    if (
        seconds_to_end > entry_seconds
        or seconds_to_end < cutoff_seconds
        or start_price <= 0
        or len(recent_spot_prices) != len(recent_sample_times)
        or not recent_spot_prices
        or confirmation_seconds < 1
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

    current_price = recent_spot_prices[-1]
    directional_move = current_price - start_price
    required_move = max(start_price * min_move_percent, min_move_usd)
    if abs(directional_move) < required_move:
        return None
    side = "UP" if directional_move > 0 else "DOWN"

    confirmation_cutoff = recent_sample_times[-1] - float(confirmation_seconds)
    prior_indices = [
        index for index, observed_at in enumerate(recent_sample_times)
        if observed_at <= confirmation_cutoff
    ]
    if not prior_indices:
        return None
    confirmation_move = current_price - recent_spot_prices[prior_indices[-1]]
    if (side == "UP" and confirmation_move <= 0) or (side == "DOWN" and confirmation_move >= 0):
        return None

    selected_quote = up_quote if side == "UP" else down_quote
    opposite_quote = down_quote if side == "UP" else up_quote
    if selected_quote.bid <= opposite_quote.bid:
        return None
    entry = selected_quote.ask
    if entry < min_entry or entry > max_entry:
        return None

    return AutoTradeSignal(
        side=side,
        token_id=market.token_ids[0] if side == "UP" else market.token_ids[1],
        price=entry,
        reason=(
            f"momentum_confirmation side={side} move_usd={directional_move.quantize(Decimal('0.01'))} "
            f"confirmation_move={confirmation_move.quantize(Decimal('0.01'))} "
            f"confirmation_seconds={confirmation_seconds} "
            f"entry={entry} "
            f"book_skew={(selected_quote.bid - opposite_quote.bid).quantize(Decimal('0.0001'))} "
            f"seconds_left={int(seconds_to_end)}"
        ),
    )


def choose_fair_value_scratch_signal(
    market: Market,
    probability_up: Decimal,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    seconds_to_end: Decimal,
    entry_seconds: Decimal = Decimal("180"),
    cutoff_seconds: Decimal = Decimal("25"),
    min_entry: Decimal = Decimal("0.45"),
    max_entry: Decimal = Decimal("0.55"),
    min_probability: Decimal = Decimal("0.53"),
    min_net_edge: Decimal = Decimal("0.02"),
    fee_rate: Decimal = Decimal("0.07"),
    max_spread: Decimal = Decimal("0.05"),
    min_ask_sum: Decimal = Decimal("0.90"),
    max_ask_sum: Decimal = Decimal("1.10"),
    probability_shrinkage: Decimal = Decimal("1"),
) -> AutoTradeSignal | None:
    """Strategy B: calibrated fair value plus a fee-adjusted edge gate."""
    if seconds_to_end > entry_seconds or seconds_to_end < cutoff_seconds:
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

    calibrated_up = shrink_probability_toward_even(
        probability_up,
        probability_shrinkage,
    )
    candidates: list[tuple[Decimal, str, str, Decimal, Decimal]] = []
    for side, token_id, quote, probability in (
        ("UP", market.token_ids[0], up_quote, calibrated_up),
        ("DOWN", market.token_ids[1], down_quote, Decimal("1") - calibrated_up),
    ):
        entry = quote.ask
        if entry < min_entry or entry > max_entry or probability < min_probability:
            continue
        fee_per_share = fee_rate * entry * (Decimal("1") - entry)
        net_edge = probability - entry - fee_per_share
        if net_edge >= min_net_edge:
            candidates.append((net_edge, side, token_id, entry, probability))
    if not candidates:
        return None
    net_edge, side, token_id, entry, probability = max(candidates, key=lambda item: item[0])
    return AutoTradeSignal(
        side=side,
        token_id=token_id,
        price=entry,
        reason=(
            f"fair_value_scratch side={side} entry={entry} "
            f"probability={probability.quantize(Decimal('0.0001'))} "
            f"net_edge={net_edge.quantize(Decimal('0.0001'))} "
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
            edge_quality * Decimal("50")
            + trend_quality * Decimal("10")
            + market_quality * Decimal("20")
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
        edge=edge_quality * Decimal("50"),
        trend=trend_quality * Decimal("10"),
        market=market_quality * Decimal("20"),
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


def response_sell_fill_amounts(
    response: dict[str, Any],
    fallback_price: Decimal,
    fallback_shares: Decimal,
) -> tuple[Decimal, Decimal]:
    try:
        shares = Decimal(str(response.get("makingAmount") or "0"))
        proceeds = Decimal(str(response.get("takingAmount") or "0"))
    except (ArithmeticError, ValueError):
        shares = Decimal("0")
        proceeds = Decimal("0")
    if shares <= 0:
        shares = fallback_shares
    if proceeds <= 0:
        proceeds = fallback_price * shares
    return proceeds, shares


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


def close_fast_simple_paper_position(
    positions: list[PaperPosition],
    bankroll: Decimal,
    market_slug: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
    fee_rate: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    remaining = quantity
    total_shares = Decimal("0")
    total_proceeds = Decimal("0")
    for position in positions:
        if position.slug != market_slug or position.side != side or position.settled:
            continue
        available = max(Decimal("0"), position.shares - position.exit_shares)
        sold = min(available, remaining)
        if sold <= 0:
            continue
        proceeds = sold * price
        fee = sold * fee_rate * price * (Decimal("1") - price)
        position.exit_shares += sold
        position.exit_proceeds += proceeds
        position.exit_fee += fee
        bankroll += proceeds - fee
        total_shares += sold
        total_proceeds += proceeds
        remaining -= sold
        if remaining <= 0:
            break
    logger.info(
        "PAPER_RISK_EXIT slug=%s side=%s price=%s shares=%s proceeds=%s bankroll=%s",
        market_slug,
        side,
        price,
        total_shares.quantize(Decimal("0.0001")),
        total_proceeds.quantize(Decimal("0.0001")),
        bankroll.quantize(Decimal("0.0001")),
    )
    return bankroll, total_shares, total_proceeds


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
        position.winner = winner
        remaining_shares = max(Decimal("0"), position.shares - position.exit_shares)
        payout = remaining_shares if position.side == winner else Decimal("0")
        bankroll += payout
        profit = (
            payout
            + position.exit_proceeds
            - position.stake
            - position.fee
            - position.exit_fee
        )
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
    now_epoch = int(time.time())
    slugs = sorted(
        {
            position.slug
            for position in positions
            if not position.settled
            and paper_market_has_ended(position.slug, now_epoch)
        }
    )
    for slug in slugs:
        bankroll = settle_paper_positions(positions, slug, bankroll)
    return bankroll


def record_fast_simple_paper_settlements(
    engine: FastDirectionalHedgeSimpleEngine,
    positions: list[PaperPosition],
) -> None:
    settled = {
        (position.slug, position.winner)
        for position in positions
        if position.settled and position.winner in {"UP", "DOWN"}
    }
    for slug, winner in settled:
        assert winner is not None
        engine.record_settlement(slug, winner)


def paper_market_has_ended(slug: str, now_epoch: int) -> bool:
    """Avoid querying Gamma for a paper position before its window closes."""
    raw_epoch = slug.rpartition("-")[2]
    if not raw_epoch.isdigit():
        # Preserve compatibility for manually supplied/non-recurring markets.
        return True
    return now_epoch >= int(raw_epoch) + 300


def scratch_decayed_paper_positions(
    positions: list[PaperPosition],
    bankroll: Decimal,
    slug: str,
    probability_up: Decimal,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    probability_threshold: Decimal = Decimal("0.52"),
    price_tolerance: Decimal = Decimal("0.02"),
    fee_rate: Decimal = Decimal("0.07"),
) -> Decimal:
    """Paper-close a decayed signal only when the best bid is near entry."""
    for position in positions:
        if position.slug != slug or position.settled:
            continue
        selected_probability = (
            probability_up if position.side == "UP" else Decimal("1") - probability_up
        )
        if selected_probability >= probability_threshold:
            continue
        quote = up_quote if position.side == "UP" else down_quote
        if quote is None or quote.bid is None or quote.bid < position.entry_price - price_tolerance:
            logger.info(
                "PAPER_SCRATCH_WAIT slug=%s side=%s probability=%s bid=%s entry=%s",
                slug,
                position.side,
                selected_probability,
                quote.bid if quote is not None else None,
                position.entry_price,
            )
            continue
        proceeds = position.shares * quote.bid
        exit_fee = position.shares * fee_rate * quote.bid * (Decimal("1") - quote.bid)
        bankroll += proceeds - exit_fee
        position.settled = True
        position.profit = proceeds - exit_fee - position.stake - position.fee
        logger.info(
            "PAPER_SCRATCH_EXIT slug=%s side=%s bid=%s probability=%s "
            "exit_fee=%s profit=%s bankroll=%s",
            slug,
            position.side,
            quote.bid,
            selected_probability.quantize(Decimal("0.0001")),
            exit_fee.quantize(Decimal("0.0001")),
            position.profit.quantize(Decimal("0.0001")),
            bankroll.quantize(Decimal("0.0001")),
        )
    return bankroll


def _seconds_to_start(market: Market, now: datetime) -> Decimal:
    if market.event_start_time is None:
        return Decimal("0")
    return Decimal(str((market.event_start_time - now).total_seconds()))


def _seconds_to_end(market: Market, now: datetime) -> Decimal:
    if market.end_time is None:
        return Decimal("300")
    return Decimal(str((market.end_time - now).total_seconds()))


def window_priority_initialization_complete(
    market: Market | None,
    now: datetime,
    start_price: Decimal | None,
    strategy: str,
    reversal_boundary_seeded_slug: str | None,
) -> bool:
    """Allow slow maintenance only after the current window decision path is ready."""
    if market is None or _seconds_to_start(market, now) > 0 or _seconds_to_end(market, now) <= 0:
        return False
    if start_price is None:
        return False
    if strategy in REVERSAL_STRATEGIES and reversal_boundary_seeded_slug != market.slug:
        return False
    return True


def rolling_realized_volatility(
    samples: list[tuple[float, Decimal]],
    *,
    observed_at: float,
    lookback_seconds: float = 60.0,
) -> Decimal | None:
    """Return unannualized realized volatility from timestamped BTC samples."""
    recent = [
        price
        for timestamp, price in samples
        if observed_at - lookback_seconds <= timestamp <= observed_at and price > 0
    ]
    if len(recent) < 3:
        return None
    squared_log_returns = [
        math.log(float(current / previous)) ** 2
        for previous, current in zip(recent, recent[1:])
    ]
    return Decimal(str(math.sqrt(sum(squared_log_returns))))


REVERSAL_NOTIFICATION_SAFE_STATUSES = frozenset(
    {
        "entry_complete",
        "entry_reconciled",
        "exit_complete",
        "exit_reconciled",
        "awaiting_settlement",
        "no_trigger_no_split",
        "opening_already_processed",
        "trigger_filtered_no_split",
        "first_stage_extreme_volatility_no_split",
        "unused_split_merged",
        "paper_complete",
        "dynamic_recovery_skipped",
        "profit_target_unfunded",
        "break_even_target_unfunded",
        "compact_entry_filtered",
        "compact_fak_skipped",
        "first_stage_entry_filtered",
        "first_stage_fak_skipped",
        "paper_unused_split_merged",
        "sparse_stage_observing",
        "sparse_entry_filtered",
    }
)


def reversal_notifications_may_run(status: str | None) -> bool:
    """Keep slow notification I/O off every order submission and retry path."""
    return status in REVERSAL_NOTIFICATION_SAFE_STATUSES


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
    polygon_resolution = PolygonResolutionReader(
        os.getenv("POLYGON_RPC_URL")
        or os.getenv("RPC_URL")
        or "https://polygon.drpc.org",
        timeout=5,
    )
    resolution_market_cache: dict[str, Market] = {}

    def fetch_chain_winner(result_slug: str) -> str | None:
        result_market = resolution_market_cache.get(result_slug)
        if result_market is None:
            result_market = load_updown_market(gamma, result_slug)
            if result_market is None:
                return None
            resolution_market_cache[result_slug] = result_market
            if len(resolution_market_cache) > 100:
                resolution_market_cache.pop(next(iter(resolution_market_cache)))
        return polygon_resolution.winner(
            result_market.condition_id,
            result_market.outcomes,
        )
    clob = ClobDataClient(args.clob_host, timeout=args.market_data_timeout)
    trader: ClobTradingClient | None = None
    price_client = SpotPriceClient(args.price_source, timeout=args.market_data_timeout, ws_proxy=args.ws_proxy)
    if args.crypto_resolution_mode != CryptoResolutionMode.LEGACY.value:
        # RTDS has no snapshot or replay. Keep the TWAP stream hot before the
        # first upgraded window so its exact opening boundary is already cached.
        price_client.warm_polymarket_chainlink_twap()
        price_client.warm_polymarket_chainlink_spot()
    price_to_beat_client = PolymarketPriceToBeatClient(
        timeout=args.market_data_timeout,
        proxy_url=args.price_to_beat_proxy or args.ws_proxy,
    )
    snapshot_writer = JsonlSnapshotWriter(Path(args.record_jsonl)) if args.record_jsonl else None
    slug = slug_from_value(args.slug)
    stop_at = float("inf") if args.duration == 0 else time.time() + args.duration
    current_market: Market | None = None
    current_resolution_mode = CryptoResolutionMode.LEGACY
    start_price: Decimal | None = None
    open_price_tracker = StableOpenPriceTracker(
        required_confirmations=args.official_open_confirmations,
        minimum_stable_seconds=args.official_open_stable_seconds,
    )
    last_spot_price: Decimal | None = None
    last_spot_fetched_at: float | None = None
    prices: list[Decimal] = []
    price_sample_times: list[float] = []
    volatility_prices: list[Decimal] = []
    volatility_sample_times: list[float] = []
    underlying_start_price: Decimal | None = None
    btc_volatility_samples: deque[tuple[float, Decimal]] = deque()
    reversal_entry_rv60: Decimal | None = None
    reversal_entry_rv300: Decimal | None = None
    up_ask_prices: list[Decimal] = []
    down_ask_prices: list[Decimal] = []
    book_sample_times: list[float] = []
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
    ewma_twap_settings = EwmaTwapSettings(
        lambda_per_second=Decimal(args.ewma_twap_lambda),
        realized_window_seconds=Decimal(args.ewma_twap_realized_seconds),
        ewma_weight=Decimal(args.ewma_twap_weight),
        minimum_model_edge=Decimal(args.ewma_twap_min_edge),
        half_spread_buffer=Decimal(args.ewma_twap_half_spread_buffer),
        slippage_buffer=Decimal(args.ewma_twap_slippage_buffer),
        taker_fee_rate=Decimal(args.ewma_twap_fee_rate),
        kelly_fraction=Decimal(args.ewma_twap_kelly_fraction),
        kelly_bankroll=Decimal(args.ewma_twap_kelly_bankroll),
        max_notional=Decimal(args.ewma_twap_max_notional),
        entry_start_seconds=Decimal(args.ewma_twap_entry_seconds),
        entry_cutoff_seconds=Decimal(args.ewma_twap_cutoff_seconds),
    )
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
    fair_value_fee_rate = Decimal(args.fair_value_fee_rate)
    fair_value_confirmation_min_seconds = args.fair_value_confirmation_min_seconds
    fair_value_book_trend_samples = args.fair_value_book_trend_samples
    fair_value_book_min_slope = Decimal(args.fair_value_book_min_slope)
    fair_value_book_min_relative_slope = Decimal(
        args.fair_value_book_min_relative_slope
    )
    fair_value_book_max_pullback = Decimal(args.fair_value_book_max_pullback)
    fast_hedge_settings = FastDirectionalHedgeSimpleSettings(
        entry_price_min=Decimal(args.fdh_entry_price_min),
        entry_price_max=Decimal(args.fdh_entry_price_max),
        entry_confirm_ticks=args.fdh_entry_confirm_ticks,
        entry_confirm_min_interval_ms=args.fdh_entry_confirm_min_interval_ms,
        base_position_size=Decimal(args.fdh_base_position_size),
        min_ask_gap=Decimal(args.fdh_min_ask_gap),
        max_spread=Decimal(args.fdh_max_spread),
        max_ask_sum=Decimal(args.fdh_max_ask_sum),
        max_entry_drift=Decimal(args.fdh_max_entry_drift),
        entry_max_slippage=Decimal(args.fdh_entry_max_slippage),
        initial_stop_pct=Decimal(args.fdh_initial_stop_pct),
        trailing_start_gain=Decimal(args.fdh_trailing_start_gain),
        break_even_buffer=Decimal(args.fdh_break_even_buffer),
        trailing_drawdown_pct=Decimal(args.fdh_trailing_drawdown_pct),
        stop_confirm_ticks=args.fdh_stop_confirm_ticks,
        fast_move_window_ms=args.fdh_fast_move_window_ms,
        fast_move_threshold=Decimal(args.fdh_fast_move_threshold),
        fast_stop_confirm_ticks=args.fdh_fast_stop_confirm_ticks,
        emergency_stop_penetration=Decimal(args.fdh_emergency_stop_penetration),
        hedge_max_slippage=Decimal(args.fdh_hedge_max_slippage),
        hedge_max_price=Decimal(args.fdh_hedge_max_price),
        hedge_entry_max_seconds=Decimal(args.fdh_hedge_entry_max_seconds),
        hedge_entry_min_seconds=Decimal(args.fdh_hedge_entry_min_seconds),
        exit_max_slippage=Decimal(args.fdh_exit_max_slippage),
        take_profit_net_per_share=Decimal(args.fdh_take_profit_net_per_share),
        take_profit_confirm_ticks=args.fdh_take_profit_confirm_ticks,
        max_entries_per_window=args.fdh_max_entries_per_window,
        normal_entry_max_seconds=Decimal(args.fdh_normal_entry_max_seconds),
        normal_entry_min_seconds=Decimal(args.fdh_normal_entry_min_seconds),
        stop_new_entry_time=Decimal(args.fdh_stop_new_entry_time),
        risk_only_time=Decimal(args.fdh_risk_only_time),
        fee_rate=Decimal(args.fdh_fee_rate),
        max_book_age_seconds=Decimal(args.fdh_max_book_age_seconds),
    )
    fast_hedge_engine = FastDirectionalHedgeSimpleEngine(
        settings=fast_hedge_settings,
        state_path=Path(args.fdh_state_json),
        recorder_path=Path(args.fdh_record_jsonl) if args.fdh_record_jsonl else None,
    )
    smart_score_threshold = Decimal(args.smart_score_threshold)
    smart_score_entry_seconds = Decimal(str(args.smart_score_entry_seconds))
    smart_score_cutoff_seconds = Decimal(str(args.smart_score_cutoff_seconds))
    smart_score_min_probability = Decimal(args.smart_score_min_probability)
    smart_score_fee_rate = Decimal(args.smart_score_fee_rate)
    smart_score_slippage = Decimal(args.smart_score_slippage)
    momentum_entry_seconds = Decimal(str(args.momentum_entry_seconds))
    momentum_cutoff_seconds = Decimal(str(args.momentum_cutoff_seconds))
    momentum_min_move_percent = Decimal(args.momentum_min_move_percent)
    momentum_min_move_usd = Decimal(args.momentum_min_move_usd)
    momentum_confirmation_seconds = args.momentum_confirmation_seconds
    momentum_min_entry = Decimal(args.momentum_min_entry)
    momentum_max_entry = Decimal(args.momentum_max_entry)
    momentum_fee_rate = Decimal(args.momentum_fee_rate)
    fair_scratch_entry_seconds = Decimal(str(args.fair_scratch_entry_seconds))
    fair_scratch_cutoff_seconds = Decimal(str(args.fair_scratch_cutoff_seconds))
    fair_scratch_min_entry = Decimal(args.fair_scratch_min_entry)
    fair_scratch_max_entry = Decimal(args.fair_scratch_max_entry)
    fair_scratch_min_probability = Decimal(args.fair_scratch_min_probability)
    fair_scratch_min_net_edge = Decimal(args.fair_scratch_min_net_edge)
    fair_scratch_fee_rate = Decimal(args.fair_scratch_fee_rate)
    fair_scratch_exit_probability = Decimal(args.fair_scratch_exit_probability)
    fair_scratch_price_tolerance = Decimal(args.fair_scratch_price_tolerance)
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
    manual_executor: ManualTradeExecutor | None = None
    reversal_boundary_seeded_slug: str | None = None
    reversal_terminal_book_results: dict[str, Direction] = {}
    reversal_completed_attempts = 0
    price_to_beat_next_retry_monotonic = 0.0
    price_to_beat_executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="official-price-to-beat",
    )
    atexit.register(price_to_beat_executor.shutdown, wait=False, cancel_futures=True)
    official_price_to_beat_task: tuple[str, Future[Any]] | None = None
    official_price_to_beat_verified_slug: str | None = None
    official_price_to_beat_next_retry_monotonic = 0.0
    provisional_open_price: Decimal | None = None
    provisional_open_mismatch_with_exposure = False
    hybrid_signal_owner: str | None = None
    reversal_pause_slug: str | None = None
    reversal_forced_exit_slug: str | None = None
    reversal_weekly_restart_pending = False
    maintenance_deferred_slug: str | None = None
    reversal_execution_prewarm_slug: str | None = None
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
        "fair_value_fee_rate": str(fair_value_fee_rate),
        "fair_value_confirmation_min_seconds": fair_value_confirmation_min_seconds,
        "fair_value_book_trend": {
            "samples": fair_value_book_trend_samples,
            "minimum_selected_slope_per_second": str(fair_value_book_min_slope),
            "minimum_relative_slope_per_second": str(
                fair_value_book_min_relative_slope
            ),
            "maximum_pullback": str(fair_value_book_max_pullback),
        },
        "ewma_twap_fair": {
            "lambda_per_second": str(ewma_twap_settings.lambda_per_second),
            "realized_window_seconds": str(ewma_twap_settings.realized_window_seconds),
            "ewma_weight": str(ewma_twap_settings.ewma_weight),
            "minimum_model_edge": str(ewma_twap_settings.minimum_model_edge),
            "half_spread_buffer": str(ewma_twap_settings.half_spread_buffer),
            "slippage_buffer": str(ewma_twap_settings.slippage_buffer),
            "taker_fee_rate": str(ewma_twap_settings.taker_fee_rate),
            "kelly_fraction": str(ewma_twap_settings.kelly_fraction),
            "kelly_bankroll": str(ewma_twap_settings.kelly_bankroll),
            "max_notional": str(ewma_twap_settings.max_notional),
            "entry_start_seconds": str(ewma_twap_settings.entry_start_seconds),
            "entry_cutoff_seconds": str(ewma_twap_settings.entry_cutoff_seconds),
        },
        "fast_directional_hedge_simple": {
            "version": fast_hedge_settings.version,
            "entry_price_range": [
                str(fast_hedge_settings.entry_price_min),
                str(fast_hedge_settings.entry_price_max),
            ],
            "entry_confirm_ticks": fast_hedge_settings.entry_confirm_ticks,
            "base_position_size": str(fast_hedge_settings.base_position_size),
            "initial_stop_pct": str(fast_hedge_settings.initial_stop_pct),
            "trailing_start_gain": str(fast_hedge_settings.trailing_start_gain),
            "trailing_drawdown_pct": str(fast_hedge_settings.trailing_drawdown_pct),
            "stop_confirm_ticks": fast_hedge_settings.stop_confirm_ticks,
            "fast_move_threshold": str(fast_hedge_settings.fast_move_threshold),
            "emergency_stop_penetration": str(
                fast_hedge_settings.emergency_stop_penetration
            ),
            "hedge_max_slippage": str(fast_hedge_settings.hedge_max_slippage),
            "max_entries_per_window": fast_hedge_settings.max_entries_per_window,
            "max_book_age_seconds": str(fast_hedge_settings.max_book_age_seconds),
        },
        "smart_score_threshold": str(smart_score_threshold),
        "smart_score_entry_seconds": str(smart_score_entry_seconds),
        "smart_score_cutoff_seconds": str(smart_score_cutoff_seconds),
        "smart_score_min_probability": str(smart_score_min_probability),
        "smart_score_fee_rate": str(smart_score_fee_rate),
        "smart_score_slippage": str(smart_score_slippage),
        "smart_score_trend_samples": args.smart_score_trend_samples,
        "smart_score_stability_samples": args.smart_score_stability_samples,
        "momentum_entry_seconds": str(momentum_entry_seconds),
        "momentum_cutoff_seconds": str(momentum_cutoff_seconds),
        "momentum_min_move_percent": str(momentum_min_move_percent),
        "momentum_min_move_usd": str(momentum_min_move_usd),
        "momentum_confirmation_seconds": momentum_confirmation_seconds,
        "momentum_entry_range": [str(momentum_min_entry), str(momentum_max_entry)],
        "momentum_fee_rate": str(momentum_fee_rate),
        "fair_scratch_entry_seconds": str(fair_scratch_entry_seconds),
        "fair_scratch_cutoff_seconds": str(fair_scratch_cutoff_seconds),
        "fair_scratch_entry_range": [
            str(fair_scratch_min_entry),
            str(fair_scratch_max_entry),
        ],
        "fair_scratch_min_probability": str(fair_scratch_min_probability),
        "fair_scratch_min_net_edge": str(fair_scratch_min_net_edge),
        "fair_scratch_fee_rate": str(fair_scratch_fee_rate),
        "fair_scratch_exit_probability": str(fair_scratch_exit_probability),
        "fair_scratch_price_tolerance": str(fair_scratch_price_tolerance),
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
        if args.strategy == "fast_directional_hedge_simple":
            live_summary["fast_directional_hedge_simple_metrics"] = (
                fast_hedge_engine.execution_summary()
            )
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
            fair_value_fee_rate < 0
            or fair_value_confirmation_min_seconds < 0
            or fair_value_book_trend_samples < 3
            or min(
                fair_value_book_min_slope,
                fair_value_book_min_relative_slope,
                fair_value_book_max_pullback,
            )
            < 0
        ):
            raise ValueError("Fair-value fee, book-trend, and confirmation settings are invalid")
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
            momentum_cutoff_seconds < 0
            or momentum_cutoff_seconds >= momentum_entry_seconds
            or momentum_min_move_percent < 0
            or momentum_min_move_usd < 0
            or momentum_confirmation_seconds < 1
            or not Decimal("0") < momentum_min_entry <= momentum_max_entry < Decimal("1")
            or momentum_fee_rate < 0
            or fair_scratch_cutoff_seconds < 0
            or fair_scratch_cutoff_seconds >= fair_scratch_entry_seconds
            or not Decimal("0") < fair_scratch_min_entry <= fair_scratch_max_entry < Decimal("1")
            or not Decimal("0.5") <= fair_scratch_min_probability <= Decimal("1")
            or fair_scratch_min_net_edge < 0
            or fair_scratch_fee_rate < 0
            or not Decimal("0.5") <= fair_scratch_exit_probability <= Decimal("1")
            or fair_scratch_price_tolerance < 0
        ):
            raise ValueError("Momentum and fair-scratch parameters must be valid")
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
        if args.live_trading or args.strategy in REVERSAL_STRATEGIES:
            reversal_state_path = Path(args.reversal_state_json)
            reversal_strategy = ReversalV11.load(reversal_state_path)
            reversal_setting_overrides = reversal_profile_overrides(args.strategy)
            if (
                args.reversal_first_stage_max_rv60 is not None
            ):
                reversal_setting_overrides.update(
                    first_stage_rv60_filter_enabled=True,
                    first_stage_max_rv60=Decimal(args.reversal_first_stage_max_rv60),
                )
            if (
                args.reversal_first_stage_max_rv300 is not None
            ):
                reversal_setting_overrides.update(
                    first_stage_rv300_filter_enabled=True,
                    first_stage_max_rv300=Decimal(args.reversal_first_stage_max_rv300),
                    first_stage_rv300_persistence_ratio=Decimal(
                        args.reversal_first_stage_rv300_persistence_ratio
                    ),
                    first_stage_rv300_hard_multiplier=Decimal(
                        args.reversal_first_stage_rv300_hard_multiplier
                    ),
                )
            if reversal_setting_overrides:
                reversal_strategy.settings = replace(
                    reversal_strategy.settings,
                    **reversal_setting_overrides,
                )
            reversal_strategy.dump(reversal_state_path)
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
                                active_reversal.awaiting_window != slug
                                and active_reversal.execution_phase
                                not in {"trend_exit_complete", "direct_entry_complete"}
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
                    # Later recovery stages are sized from live fills and asks, so a
                    # fixed whole-round capital threshold cannot prove affordability.
                    # At startup, require enough collateral for the next executable
                    # stage; each later order remains constrained by actual balance.
                    required_reversal_collateral = reversal_strategy.settings.stakes[0]
                elif (
                    active_reversal.awaiting_window is not None
                    and active_reversal.awaiting_window != slug
                    and active_reversal.execution_phase
                    in {"trend_exit_complete", "direct_entry_complete"}
                    and active_reversal.failures + 1
                    < reversal_strategy.settings.attempt_limit
                ):
                    required_reversal_collateral = reversal_strategy.settings.stakes[
                        active_reversal.failures + 1
                    ]
                elif active_reversal.awaiting_window is not None and active_reversal.execution_phase in {
                    "split_confirmed",
                    "trend_exit_partial",
                    "trend_exit_submitting",
                    "trend_exit_complete",
                    "direct_entry_ready",
                    "direct_entry_partial",
                    "direct_entry_submitting",
                    "direct_entry_complete",
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
                    execution_mode="direct_buy",
                    wallet=os.getenv(args.funder_address_env) or "CLOB funder",
                    require_trade_collateral=not notifications.trading_paused,
                )
                live_summary["reversal_startup_self_check"] = {
                    "wallet": startup_report.wallet,
                    "collateral_units": startup_report.collateral_units,
                    "open_orders": startup_report.open_orders,
                    "up_balance": str(startup_report.up_balance),
                    "down_balance": str(startup_report.down_balance),
                    "relayer_deployed": startup_report.relayer_deployed,
                }
                startup_spot = price_client.btc_usd()
                if startup_spot.observed_at is None:
                    raise ValueError("Chainlink startup self-check returned no timestamp")
                startup_spot_age = abs(int(time.time()) - startup_spot.observed_at)
                if startup_spot_age > args.max_spot_age:
                    raise ValueError(
                        f"Chainlink startup self-check price is stale by {startup_spot_age}s"
                    )
                live_summary["reversal_startup_self_check"].update(
                    {
                        "spot_source": startup_spot.source,
                        "spot_price": str(startup_spot.price),
                        "spot_age_seconds": startup_spot_age,
                    }
                )
            reversal_runtime = ReversalRuntime(
                strategy=reversal_strategy,
                state_path=reversal_state_path,
                winner_lookup=fetch_winner,
                splitter=reversal_splitter,
                trader=trader,
                signature_type=args.signature_type,
                live=args.live_trading,
                execution_mode="direct_buy",
                order_callback=notifications.record_reversal_exit,
                chain_winner_lookup=fetch_chain_winner,
                unlocked_profit_lookup=notifications.reversal_unlocked_profit,
            )
        if args.live_trading:
            assert trader is not None
            if args.strategy == "fast_directional_hedge_simple":
                startup_market = load_updown_market(gamma, slug)
                if startup_market is None:
                    raise ValueError("fast directional hedge startup could not load current market")
                startup_books = clob.books(startup_market.token_ids)
                if len(startup_books) != 2:
                    raise ValueError("fast directional hedge startup did not receive both books")
                open_orders = trader.open_orders()
                relevant_open_orders = [
                    order
                    for order in open_orders
                    if str(
                        (order.get("asset_id") or order.get("assetId") or "")
                        if isinstance(order, dict)
                        else getattr(order, "asset_id", "")
                    )
                    in startup_market.token_ids
                ]
                if relevant_open_orders:
                    raise RuntimeError(
                        "fast directional hedge has unresolved current-market open orders"
                    )
                startup_up_balance = trader.conditional_balance(
                    startup_market.token_ids[0], args.signature_type
                )
                startup_down_balance = trader.conditional_balance(
                    startup_market.token_ids[1], args.signature_type
                )
                fast_hedge_engine.reconcile_positions(
                    startup_up_balance,
                    startup_down_balance,
                )
                live_summary["fast_directional_hedge_simple_startup_self_check"] = {
                    "market": startup_market.slug,
                    "open_orders": 0,
                    "up_balance": str(startup_up_balance),
                    "down_balance": str(startup_down_balance),
                    "collateral": str(
                        trader.refresh_collateral_balance(args.signature_type)
                    ),
                    "state_reconciled": True,
                }
            manual_trader = build_live_trader(args)
            manual_executor = ManualTradeExecutor(
                trader=manual_trader,
                signature_type=args.signature_type,
                market_loader=gamma.market_by_slug,
                book_loader=clob.books,
                on_submitting=notifications.mark_manual_submitting,
                on_result=notifications.record_manual_result,
                buy_slippage=live_buy_slippage,
                sell_slippage=live_buy_slippage,
            )
            manual_executor.start()
            notifications.attach_manual_executor(manual_executor.submit)
            atexit.register(manual_executor.stop)
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

    def run_slow_notification_maintenance(maintenance_slug: str) -> None:
        nonlocal maintenance_deferred_slug, reversal_weekly_restart_pending

        if maintenance_deferred_slug is not None:
            logger.info(
                "WINDOW_PRIORITY_READY slug=%s; running deferred settlements and reports",
                maintenance_slug,
            )
            maintenance_deferred_slug = None
        try:
            notifications.maybe_delete_expired_discord_messages()
            notifications.maybe_send_settlements(fetch_winner)
            additional_report = None
            on_reported = None
            if reversal_runtime is not None and args.strategy in REVERSAL_STRATEGIES:
                additional_report = reversal_runtime.daily_report_text
                on_reported = reversal_runtime.mark_daily_report_sent
            report_day = notifications.maybe_send_daily(
                fetch_winner,
                additional_report=additional_report,
                on_reported=on_reported,
            )
            if report_day is not None and reversal_runtime is not None:
                if weekly_restart_report_day(report_day):
                    reversal_weekly_restart_pending = True
        except Exception as exc:
            # Notification maintenance must never alter the trading state or
            # interrupt the next order attempt.
            logger.warning(
                "NOTIFICATION_MAINTENANCE_FAILED slug=%s error=%s",
                maintenance_slug,
                exc,
                exc_info=True,
            )

    def restart_with_current_window(status: str, reason: str) -> None:
        restart_slug = current_5m_slug(slug)
        live_summary["status"] = status
        live_summary["restart_slug"] = restart_slug
        write_live_summary()
        notifications.stop(reason)
        restart_argv = argv_with_current_slug(sys.argv[1:], restart_slug)
        os.execv(
            sys.executable,
            [sys.executable, "-m", "src.watch_updown", *restart_argv],
        )

    def maybe_execute_weekly_restart() -> None:
        if not reversal_weekly_restart_pending or reversal_runtime is None:
            return
        state = reversal_runtime.strategy.state
        if not weekly_restart_is_safe(state):
            return
        restart_with_current_window(
            "weekly_safe_restart",
            "反转策略每周安全重启",
        )

    control_sleep_logged = False
    while time.time() < stop_at:
        iteration_started_at = time.monotonic()
        poll_interval = float(args.interval)
        now = datetime.now(timezone.utc)
        notifications.update_runtime()
        if notifications.process_commands():
            notifications.prepare_restart()
            restart_with_current_window("restarting", "手动重启")
        if args.live_trading and notifications.trading_paused:
            if not control_sleep_logged:
                logger.warning(
                    "LIVE_CONTROL_SLEEP trading, market, settlement, and strategy "
                    "monitoring are suspended; Telegram commands remain available"
                )
                control_sleep_logged = True
            sleep_until_next_poll(min(5.0, max(1.0, poll_interval)), iteration_started_at)
            continue
        if control_sleep_logged:
            logger.warning(
                "LIVE_CONTROL_RESUME reconnecting to the current market after control sleep"
            )
            control_sleep_logged = False
        maintenance_ready = window_priority_initialization_complete(
            current_market,
            now,
            start_price,
            args.strategy,
            reversal_boundary_seeded_slug,
        )
        maintenance_slug = current_market.slug if current_market is not None else slug
        if maintenance_ready and args.strategy not in REVERSAL_STRATEGIES:
            run_slow_notification_maintenance(maintenance_slug)
        elif maintenance_deferred_slug != maintenance_slug:
            maintenance_deferred_slug = maintenance_slug
            logger.info(
                "WINDOW_PRIORITY_DEFERRED slug=%s; settlements and reports wait for the order path",
                maintenance_slug,
            )
        maybe_execute_weekly_restart()
        if args.paper_trading:
            paper_bankroll = settle_all_paper_positions(paper_positions, paper_bankroll)
            if args.strategy == "fast_directional_hedge_simple":
                record_fast_simple_paper_settlements(fast_hedge_engine, paper_positions)
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
                reversal_entry_rv60 = rolling_realized_volatility(
                    list(btc_volatility_samples),
                    observed_at=time.time(),
                )
                reversal_entry_rv300 = rolling_realized_volatility(
                    list(btc_volatility_samples),
                    observed_at=time.time(),
                    lookback_seconds=300.0,
                )
                logger.info(
                    "REVERSAL_VOLATILITY_PRECOMPUTED completed_slug=%s "
                    "rv60=%s rv60_threshold=%s rv300=%s rv300_threshold=%s",
                    current_market.slug,
                    reversal_entry_rv60,
                    args.reversal_first_stage_max_rv60,
                    reversal_entry_rv300,
                    args.reversal_first_stage_max_rv300,
                )
                slug = next_5m_slug(current_market.slug)
                logger.info("Window ended. Looking for next slug: %s", slug)
            wall_clock_slug = current_5m_slug(slug, now)
            if int(slug.rpartition("-")[2]) < int(wall_clock_slug.rpartition("-")[2]):
                logger.warning(
                    "WINDOW_FAST_FORWARD stale_slug=%s current_slug=%s; "
                    "missing windows will not be replayed as live windows",
                    slug,
                    wall_clock_slug,
                )
                slug = wall_clock_slug
            current_market = load_updown_market(gamma, slug)
            start_price = None
            price_to_beat_next_retry_monotonic = 0.0
            official_price_to_beat_task = None
            official_price_to_beat_verified_slug = None
            official_price_to_beat_next_retry_monotonic = 0.0
            provisional_open_price = None
            provisional_open_mismatch_with_exposure = False
            hybrid_signal_owner = None
            reversal_boundary_seeded_slug = None
            reversal_completed_attempts = 0
            open_price_tracker.reset()
            # Direction/confirmation/momentum samples are window-local. The
            # separate btc_volatility_samples deque is deliberately preserved
            # across this reset and may be used only for volatility estimation.
            prices = []
            price_sample_times = []
            volatility_prices = []
            volatility_sample_times = []
            underlying_start_price = None
            up_ask_prices = []
            down_ask_prices = []
            book_sample_times = []
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
            current_resolution_mode = detect_crypto_resolution_mode(
                current_market.rules_text,
                args.crypto_resolution_mode,
            )
            if (
                current_resolution_mode is CryptoResolutionMode.TWAP_60
                and not has_official_btc_5m_twap_rule(current_market.rules_text)
            ):
                notifications.notify_exception(
                    "读取 Polymarket 结算规则",
                    RuntimeError(
                        f"{current_market.slug} does not publish the expected "
                        "Chainlink BTC/USD TWAP 60s resolution source"
                    ),
                    key=f"resolution-rule:{current_market.slug}",
                )
                logger.error(
                    "CRYPTO_RESOLUTION_RULE_UNVERIFIED slug=%s; "
                    "window fails closed without spot/order-book fallback",
                    current_market.slug,
                )
                slug = next_5m_slug(current_market.slug)
                current_market = None
                continue
            if reversal_runtime is not None:
                reversal_boundary_state_mode = (
                    "twap60_chainlink_boundary_v3"
                    if current_resolution_mode is CryptoResolutionMode.TWAP_60
                    else current_resolution_mode.value
                )
                changed = reversal_runtime.set_boundary_price_mode(
                    reversal_boundary_state_mode
                )
                if changed:
                    logger.warning(
                        "REVERSAL_BOUNDARY_MODE_CHANGED slug=%s mode=%s; old boundary cache cleared",
                        current_market.slug,
                        reversal_boundary_state_mode,
                    )
                if (
                    current_resolution_mode is CryptoResolutionMode.TWAP_60
                    and reversal_runtime.strategy.state.last_settled_slug
                    == previous_5m_slug(current_market.slug)
                ):
                    reversal_boundary_seeded_slug = current_market.slug
            if args.crypto_resolution_mode == "auto" and not current_market.rules_text:
                logger.warning(
                    "CRYPTO_RESOLUTION_RULES_MISSING slug=%s; retaining legacy mode",
                    current_market.slug,
                )
            logger.info(
                "CRYPTO_RESOLUTION_MODE slug=%s mode=%s configured=%s",
                current_market.slug,
                current_resolution_mode.value,
                args.crypto_resolution_mode,
            )
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
            if reversal_runtime is not None and args.strategy in REVERSAL_STRATEGIES:
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
                if (
                    reversal_runtime is not None
                    and activated_strategy in REVERSAL_STRATEGIES
                ):
                    activated_settings = ReversalV11().settings
                    activated_setting_overrides = reversal_profile_overrides(
                        activated_strategy
                    )
                    if (
                        args.reversal_first_stage_max_rv60 is not None
                    ):
                        activated_setting_overrides.update(
                            first_stage_rv60_filter_enabled=True,
                            first_stage_max_rv60=Decimal(
                                args.reversal_first_stage_max_rv60
                            ),
                        )
                    if (
                        args.reversal_first_stage_max_rv300 is not None
                    ):
                        activated_setting_overrides.update(
                            first_stage_rv300_filter_enabled=True,
                            first_stage_max_rv300=Decimal(
                                args.reversal_first_stage_max_rv300
                            ),
                            first_stage_rv300_persistence_ratio=Decimal(
                                args.reversal_first_stage_rv300_persistence_ratio
                            ),
                            first_stage_rv300_hard_multiplier=Decimal(
                                args.reversal_first_stage_rv300_hard_multiplier
                            ),
                        )
                    reversal_runtime.strategy.settings = replace(
                        activated_settings,
                        **activated_setting_overrides,
                    )
            if args.strategy == "fast_directional_hedge_simple":
                fast_hedge_engine.begin_market(current_market.slug)
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

        # Warm the one likely retained token and the collateral snapshot before
        # the window boundary. This keeps metadata and balance HTTP calls out of
        # the order-critical path without polling every market unnecessarily.
        if (
            args.live_trading
            and args.strategy in REVERSAL_STRATEGIES
            and reversal_runtime is not None
            and trader is not None
            and Decimal("0") < seconds_to_end <= Decimal("15")
        ):
            next_order_slug = next_5m_slug(current_market.slug)
            if reversal_execution_prewarm_slug != next_order_slug:
                reversal_state = reversal_runtime.strategy.state
                likely_trend_side: Direction | None = None
                if reversal_state.active_round is not None:
                    likely_trend_side = reversal_state.active_round.trend_side
                else:
                    needed = reversal_runtime.strategy.settings.trigger_streak - 1
                    recent = reversal_state.recent_results[-needed:] if needed > 0 else []
                    if recent and len(recent) == needed and len(set(recent)) == 1:
                        likely_trend_side = recent[-1]
                if likely_trend_side is not None:
                    # Mark attempted first: a transient prefetch failure must not
                    # block every final-second loop or compete with boundary work.
                    reversal_execution_prewarm_slug = next_order_slug
                    try:
                        next_order_market = load_updown_market(gamma, next_order_slug)
                        if next_order_market is None:
                            raise LookupError(
                                f"next reversal market unavailable: {next_order_slug}"
                            )
                        retained_index = 1 if likely_trend_side is Direction.UP else 0
                        retained_token = next_order_market.token_ids[retained_index]
                        trader.prewarm_market_order_metadata((retained_token,))
                        prefetched_collateral = trader.refresh_collateral_balance(
                            args.signature_type
                        )
                        logger.info(
                            "REVERSAL_EXECUTION_PREWARM slug=%s token=%s "
                            "collateral=%s seconds_left=%s",
                            next_order_slug,
                            retained_token,
                            prefetched_collateral,
                            seconds_to_end,
                        )
                    except Exception as exc:
                        logger.warning(
                            "REVERSAL_EXECUTION_PREWARM_FAILED slug=%s error=%s; "
                            "normal order-path lookup remains available",
                            next_order_slug,
                            exc,
                        )
        poll_interval = polling_interval_for_seconds_left(
            seconds_to_end,
            float(args.interval),
            args.final_poll_seconds,
            args.final_poll_interval,
        )
        if (
            args.strategy in REVERSAL_STRATEGIES
            and reversal_boundary_seeded_slug != current_market.slug
            and reversal_completed_attempts < REVERSAL_COMPLETED_FAST_ATTEMPTS
            and seconds_to_start <= 0
        ):
            poll_interval = min(poll_interval, 1.0)
        if primary_side_this_window is not None:
            poll_interval = min(poll_interval, args.post_fill_poll_interval)
        if (
            args.strategy in REVERSAL_STRATEGIES
            and reversal_runtime is not None
            and seconds_to_start <= 0
            and reversal_boundary_seeded_slug != current_market.slug
            and reversal_runtime.strategy.state.prepared_split is None
            and reversal_runtime.execution_mode == "split_sell"
            and not notifications.trading_paused
            and reversal_pause_slug != current_market.slug
        ):
            try:
                presplit_result = (
                    reversal_runtime.prepare_active_round_opening_split(
                        current_market
                    )
                )
                if presplit_result is not None:
                    logger.info(
                        "REVERSAL_OPENING_PRESPLIT slug=%s status=%s detail=%s",
                        current_market.slug,
                        presplit_result.status,
                        presplit_result.detail,
                    )
            except Exception as exc:
                notifications.notify_exception(
                    f"反转策略开盘预拆 {current_market.slug}",
                    exc,
                    key=f"reversal-presplit:{current_market.slug}",
                    cooldown=0,
                )
                prepared_reversal = reversal_runtime.strategy.state.prepared_split
                if (
                    prepared_reversal is not None
                    and prepared_reversal.execution_phase
                    in {"split_submitting", "split_uncertain"}
                ):
                    reversal_pause_slug = current_market.slug
                    logger.error(
                        "REVERSAL_PRESPLIT_WINDOW_PAUSE_SET slug=%s; "
                        "global trading remains enabled",
                        current_market.slug,
                    )
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
        try:
            if current_resolution_mode is CryptoResolutionMode.TWAP_60:
                spot = price_client.polymarket_chainlink_twap()
                underlying_spot = price_client.btc_usd()
            else:
                spot = price_client.btc_usd()
                underlying_spot = spot
            if spot.observed_at is not None:
                report_age = abs(int(time.time()) - spot.observed_at)
                if report_age > args.max_spot_age:
                    raise RuntimeError(f"Chainlink report is stale by {report_age}s")
            if underlying_spot.observed_at is not None:
                underlying_age = abs(int(time.time()) - underlying_spot.observed_at)
                if underlying_age > args.max_spot_age:
                    raise RuntimeError(
                        f"Underlying Chainlink report is stale by {underlying_age}s"
                    )
            last_spot_price = spot.price
            last_spot_fetched_at = time.monotonic()
        except Exception as exc:
            if "Chainlink report is stale by" in str(exc):
                logger.warning(
                    "CHAINLINK_STALE_NOTIFICATION_SUPPRESSED "
                    "context=Chainlink BTC 行情 error=%s",
                    exc,
                )
            else:
                notifications.notify_exception("Chainlink BTC 行情", exc, key="spot-price")
            if current_resolution_mode is CryptoResolutionMode.TWAP_60:
                logger.warning(
                    "TWAP price unavailable; current window fails closed without legacy/cache fallback: %s",
                    exc,
                )
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            if last_spot_price is None:
                logger.warning("Spot price unavailable and no cached price exists: %s", exc)
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            logger.warning("Spot price unavailable; reusing cached BTC/USD=%s: %s", last_spot_price, exc)
            spot = type("CachedSpotPrice", (), {"price": last_spot_price, "source": "CACHE"})()
            underlying_spot = spot

        spot_age = Decimal(str(time.monotonic() - last_spot_fetched_at)) if last_spot_fetched_at is not None else Decimal("Infinity")
        notifications.update_runtime(
            slug=current_market.slug,
            seconds_left=seconds_to_end,
            spot=spot.price,
            spot_source=spot.source,
        )
        volatility_observed_at = time.time()
        if spot.source != "CACHE":
            btc_volatility_samples.append((volatility_observed_at, underlying_spot.price))
            while (
                btc_volatility_samples
                and volatility_observed_at - btc_volatility_samples[0][0] > 300
            ):
                btc_volatility_samples.popleft()

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
            args.strategy in REVERSAL_STRATEGIES
            and reversal_runtime is not None
            and reversal_boundary_seeded_slug != current_market.slug
            and not notifications.trading_paused
            and reversal_completed_attempts < REVERSAL_COMPLETED_FAST_ATTEMPTS
            and current_resolution_mode is CryptoResolutionMode.LEGACY
        ):
            reversal_completed_attempts += 1
            try:
                completed_prices = fetch_reversal_completed_window_prices(
                    price_to_beat_client,
                    current_market,
                    reversal_runtime.strategy.state.last_settled_slug,
                    reversal_runtime.strategy.settings.trigger_streak,
                )
                completed_outcomes = (
                    reversal_runtime.observe_completed_window_prices(completed_prices)
                )
                latest_completed_slug = previous_5m_slug(current_market.slug)
                if (
                    reversal_runtime.strategy.state.last_settled_slug
                    == latest_completed_slug
                ):
                    reversal_boundary_seeded_slug = current_market.slug
                    logger.info(
                        "REVERSAL_RESULT_READY slug=%s source=completed_open_close",
                        latest_completed_slug,
                    )
                for result_slug, settlement_status in completed_outcomes:
                    result = reversal_runtime.strategy.state.pending_gamma_results.get(
                        result_slug
                    )
                    logger.info(
                        "REVERSAL_COMPLETED_RESULT slug=%s result=%s status=%s "
                        "source=completed_open_close",
                        result_slug,
                        result.value if result is not None else "already_settled",
                        settlement_status,
                    )
            except GammaResultMismatch as exc:
                reversal_runtime.quarantine_gamma_mismatch(exc)
                reversal_pause_slug = current_market.slug
                notifications.notify_exception(
                    f"反转策略完成态冲突 {current_market.slug}",
                    exc,
                    key=f"reversal-completed-mismatch:{current_market.slug}",
                    cooldown=0,
                )
                logger.error(
                    "REVERSAL_COMPLETED_MISMATCH slug=%s result_slug=%s error=%s; "
                    "current window paused",
                    current_market.slug,
                    exc.slug,
                    exc,
                )
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            except Exception as exc:
                logger.warning(
                    "REVERSAL_COMPLETED_PENDING slug=%s attempt=%s/%s error=%s; "
                    "Price-to-Beat boundary fallback follows immediately",
                    current_market.slug,
                    reversal_completed_attempts,
                    REVERSAL_COMPLETED_FAST_ATTEMPTS,
                    exc,
                )
            if reversal_boundary_seeded_slug != current_market.slug:
                if (
                    reversal_completed_attempts
                    >= REVERSAL_COMPLETED_FAST_ATTEMPTS
                ):
                    logger.warning(
                        "REVERSAL_COMPLETED_FAST_PATH_EXHAUSTED slug=%s; "
                        "switching to Price-to-Beat boundary fallback",
                        current_market.slug,
                    )
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue

        if args.strategy == "open_060":
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
                    available_depth = (
                        executable_bid_depth(selected_book, signal.price)
                        if signal.action == "SELL"
                        else executable_ask_depth(selected_book, signal.price)
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

        if start_price is None and args.strategy == "fast_directional_hedge_simple":
            # This strategy has no strike, TWAP or fair-probability input. Keep
            # a harmless diagnostic reference so the shared loop can continue,
            # but never wait for a historical boundary sample or official
            # Price to Beat before evaluating the live books.
            start_price = underlying_spot.price
            underlying_start_price = underlying_spot.price
            prices = [spot.price]
            price_sample_times = [time.monotonic()]
            volatility_prices = [underlying_spot.price]
            volatility_sample_times = [time.monotonic()]
            logger.info(
                "FAST_DIRECTIONAL_HEDGE_SIMPLE_BOOK_ONLY_READY slug=%s diagnostic_reference=%s",
                current_market.slug,
                start_price,
            )

        if (
            start_price is None
            and current_resolution_mode is CryptoResolutionMode.TWAP_60
        ):
            elapsed_since_start = -seconds_to_start
            boundary_timestamp_ms = int(current_market.event_start_time.timestamp() * 1000)
            boundary_spot = None
            opening_boundary_spot = None
            endpoint_price_to_beat = None
            persisted_open_price = (
                reversal_runtime.strategy.state.chainlink_open_prices.get(
                    current_market.slug
                )
                if reversal_runtime is not None
                and args.strategy in REVERSAL_STRATEGIES
                else None
            )
            if time.monotonic() < price_to_beat_next_retry_monotonic:
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            use_chainlink_provisional_open = True
            # Every TWAP-mode strategy can begin from the first Polymarket
            # Chainlink RTDS tick at/after the exact window boundary. The
            # official openPrice remains authoritative and is fetched
            # asynchronously for reconciliation outside the order-critical path.
            if persisted_open_price is None:
                try:
                    opening_boundary_spot = (
                        price_client.polymarket_chainlink_price_at_or_after(
                            boundary_timestamp_ms,
                            args.max_boundary_sample_offset_ms,
                        )
                        if use_chainlink_provisional_open
                        else price_client.polymarket_chainlink_price_near(
                            boundary_timestamp_ms,
                            args.max_boundary_sample_offset_ms,
                        )
                    )
                except Exception as opening_boundary_exc:
                    logger.warning(
                        "CHAINLINK_OPEN_BOUNDARY_PENDING slug=%s error=%s; %s",
                        current_market.slug,
                        opening_boundary_exc,
                        (
                            "strategy waits for a bounded post-boundary RTDS tick"
                            if use_chainlink_provisional_open
                            else "official Price to Beat requires stability confirmation"
                        ),
                    )

            if use_chainlink_provisional_open:
                if opening_boundary_spot is None and persisted_open_price is None:
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                boundary_source = (
                    "persisted_chainlink_open_price"
                    if persisted_open_price is not None
                    else "chainlink_rtds_provisional"
                )
                confirmed_open_price = (
                    persisted_open_price
                    if persisted_open_price is not None
                    else opening_boundary_spot.price
                )
                provisional_open_price = confirmed_open_price
                if official_price_to_beat_task is None:
                    official_price_to_beat_task = (
                        current_market.slug,
                        price_to_beat_executor.submit(
                            price_to_beat_client.fetch,
                            current_market.event_start_time,
                            current_market.end_time,
                        ),
                    )
                    logger.info(
                        "OFFICIAL_PRICE_TO_BEAT_RECONCILE_STARTED slug=%s provisional=%s",
                        current_market.slug,
                        confirmed_open_price,
                    )
            else:
                try:
                    endpoint_price_to_beat = price_to_beat_client.fetch(
                        current_market.event_start_time,
                        current_market.end_time,
                    )
                except Exception as price_to_beat_exc:
                    logger.warning(
                        "OFFICIAL_PRICE_TO_BEAT_PENDING slug=%s error=%s; "
                        "TWAP mode fails closed without the published Price to Beat",
                        current_market.slug,
                        price_to_beat_exc,
                    )
                    price_to_beat_next_retry_monotonic = time.monotonic() + (
                        3.0 if is_http_rate_limit(price_to_beat_exc) else 1.0
                    )
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                price_to_beat_next_retry_monotonic = 0.0

                if opening_boundary_spot is not None:
                    opening_alignment_difference = abs(
                        endpoint_price_to_beat.price_to_beat - opening_boundary_spot.price
                    )
                    if opening_alignment_difference > max_price_alignment_difference:
                        open_price_tracker.reset()
                        logger.warning(
                            "OFFICIAL_PRICE_TO_BEAT_UNVERIFIED slug=%s official=%s "
                            "chainlink_open=%s difference=%s max_difference=%s; retrying",
                            current_market.slug,
                            endpoint_price_to_beat.price_to_beat,
                            opening_boundary_spot.price,
                            opening_alignment_difference,
                            max_price_alignment_difference,
                        )
                        price_to_beat_next_retry_monotonic = time.monotonic() + 1.0
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    boundary_source = "official_price_to_beat_chainlink_verified"
                    confirmed_open_price = endpoint_price_to_beat.price_to_beat
                else:
                    boundary_source = "official_price_to_beat_stability_verified"
                    # When the independent boundary sample is unavailable, reuse
                    # the fair-value strategy's stable official-open fallback.
                    confirmed_open_price = accept_open_price(
                        args.strategy,
                        open_price_tracker,
                        endpoint_price_to_beat.price_to_beat,
                        time.monotonic(),
                    )
                if confirmed_open_price is None:
                    stable_elapsed = (
                        0.0
                        if open_price_tracker.candidate_since is None
                        else max(
                            0.0,
                            time.monotonic() - open_price_tracker.candidate_since,
                        )
                    )
                    price_to_beat_next_retry_monotonic = time.monotonic() + max(
                        1.0,
                        open_price_tracker.minimum_stable_seconds - stable_elapsed,
                    )
                    logger.info(
                        "OFFICIAL_PRICE_TO_BEAT_STABILITY_PENDING slug=%s candidate=%s "
                        "confirmations=%s/%s stable=%.1f/%.1fs incomplete=%s",
                        current_market.slug,
                        open_price_tracker.candidate,
                        open_price_tracker.confirmations,
                        open_price_tracker.required_confirmations,
                        stable_elapsed,
                        open_price_tracker.minimum_stable_seconds,
                        endpoint_price_to_beat.incomplete,
                    )
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
            start_price = confirmed_open_price
            if use_chainlink_provisional_open:
                logger.info(
                    "CHAINLINK_PROVISIONAL_PRICE_TO_BEAT_FIXED slug=%s price=%s "
                    "capture_delay=%ss offset_ms=%s",
                    current_market.slug,
                    start_price,
                    elapsed_since_start,
                    (
                        opening_boundary_spot.observed_at_ms - boundary_timestamp_ms
                        if opening_boundary_spot is not None
                        else None
                    ),
                )
            else:
                logger.info(
                    "OFFICIAL_PRICE_TO_BEAT_FIXED slug=%s price=%s "
                    "capture_delay=%ss source=%s endpoint_incomplete=%s",
                    current_market.slug,
                    start_price,
                    elapsed_since_start,
                    boundary_source,
                    (
                        endpoint_price_to_beat.incomplete
                        if endpoint_price_to_beat is not None
                        else None
                    ),
                )

            # Capture the exact same-source Chainlink 60-second TWAP boundary.
            # This is the only boundary series allowed to settle a TWAP-mode
            # reversal window. The regular RTDS price and the legacy openPrice
            # endpoint remain independent inputs for other strategies.
            try:
                boundary_spot = price_client.polymarket_chainlink_twap_near(
                    boundary_timestamp_ms,
                    args.max_boundary_sample_offset_ms,
                )
            except Exception as exc:
                boundary_spot_price = None
                boundary_offset_ms = None
                logger.warning(
                    "TWAP_SETTLEMENT_BOUNDARY_PENDING slug=%s boundary_ms=%s error=%s",
                    current_market.slug,
                    boundary_timestamp_ms,
                    exc,
                )
            else:
                boundary_spot_price = boundary_spot.price
                boundary_offset_ms = (
                    boundary_spot.observed_at_ms - boundary_timestamp_ms
                )
            if (
                reversal_runtime is not None
                and args.strategy in REVERSAL_STRATEGIES
            ):
                if boundary_spot_price is None:
                    logger.warning(
                        "REVERSAL_TWAP_BOUNDARY_PENDING slug=%s; reversal channel "
                        "fails closed until the same-source boundary is available",
                        current_market.slug,
                    )
                else:
                    reversal_runtime.record_chainlink_open_price(
                        current_market.slug,
                        boundary_spot_price,
                        allow_correction=True,
                    )
                    logger.info(
                        "REVERSAL_TWAP_BOUNDARY_READY slug=%s boundary=%s offset_ms=%s",
                        current_market.slug,
                        boundary_spot_price,
                        boundary_offset_ms,
                    )
            alignment_record = {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "slug": current_market.slug,
                "status": (
                    "CHAINLINK_PROVISIONAL_BOUNDARY"
                    if use_chainlink_provisional_open
                    else "TWAP_60_BOUNDARY"
                ),
                "boundary_source": boundary_source,
                "official_price_to_beat": (
                    str(endpoint_price_to_beat.price_to_beat)
                    if endpoint_price_to_beat is not None
                    else None
                ),
                "provisional_price_to_beat": (
                    str(start_price) if use_chainlink_provisional_open else None
                ),
                "boundary_chainlink_price": (
                    str(opening_boundary_spot.price)
                    if opening_boundary_spot is not None
                    else None
                ),
                "boundary_chainlink_timestamp_ms": (
                    opening_boundary_spot.observed_at_ms
                    if opening_boundary_spot is not None
                    else None
                ),
                "boundary_offset_ms": (
                    opening_boundary_spot.observed_at_ms - boundary_timestamp_ms
                    if opening_boundary_spot is not None
                    else None
                ),
                "alignment_difference": (
                    str(abs(endpoint_price_to_beat.price_to_beat - start_price))
                    if endpoint_price_to_beat is not None
                    else None
                ),
                "twap_settlement_boundary_price": (
                    str(boundary_spot_price)
                    if boundary_spot_price is not None
                    else None
                ),
                "boundary_error": None,
                "capture_delay_seconds": str(elapsed_since_start),
                "endpoint_timestamp_ms": (
                    endpoint_price_to_beat.timestamp_ms
                    if endpoint_price_to_beat is not None
                    else None
                ),
                "endpoint_incomplete": (
                    endpoint_price_to_beat.incomplete
                    if endpoint_price_to_beat is not None
                    else None
                ),
            }
            if boundary_spot is not None:
                logger.info(
                    "TWAP_SETTLEMENT_BOUNDARY slug=%s price=%s boundary_offset_ms=%s "
                    "capture_delay=%ss",
                    current_market.slug,
                    boundary_spot_price,
                    boundary_offset_ms,
                    elapsed_since_start,
                )
            if args.price_alignment_jsonl:
                alignment_path = Path(args.price_alignment_jsonl)
                alignment_path.parent.mkdir(parents=True, exist_ok=True)
                with alignment_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(alignment_record, ensure_ascii=True) + "\n")
            underlying_start_price = start_price
            if opening_boundary_spot is not None:
                logger.info(
                    "UNDERLYING_OPEN_BOUNDARY slug=%s price=%s offset_ms=%s",
                    current_market.slug,
                    underlying_start_price,
                    opening_boundary_spot.observed_at_ms - boundary_timestamp_ms,
                )
            prices = [spot.price]
            price_sample_times = [time.monotonic()]
            volatility_prices = [underlying_spot.price]
            volatility_sample_times = [time.monotonic()]
            logger.info(
                "Captured price_to_beat=%s for %s source=%s",
                start_price,
                current_market.slug,
                boundary_source,
            )
        elif start_price is None:
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
                confirmed_open_price = accept_open_price(
                    args.strategy,
                    open_price_tracker,
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
                    boundary_spot = (
                        price_client.polymarket_chainlink_twap_near(
                            boundary_timestamp_ms,
                            args.max_boundary_sample_offset_ms,
                        )
                        if current_resolution_mode is CryptoResolutionMode.TWAP_60
                        else price_client.polymarket_chainlink_price_near(
                            boundary_timestamp_ms,
                            args.max_boundary_sample_offset_ms,
                        )
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
            volatility_prices = [underlying_spot.price]
            volatility_sample_times = [time.monotonic()]
            underlying_start_price = (
                boundary_spot.price
                if boundary_spot is not None
                else underlying_spot.price
            )
            logger.info("Captured price_to_beat=%s for %s", start_price, current_market.slug)
        else:
            prices.append(spot.price)
            price_sample_times.append(time.monotonic())
            volatility_prices.append(underlying_spot.price)
            volatility_sample_times.append(time.monotonic())

        if (
            current_resolution_mode is CryptoResolutionMode.TWAP_60
            and provisional_open_price is not None
            and official_price_to_beat_verified_slug != current_market.slug
        ):
            if (
                official_price_to_beat_task is not None
                and official_price_to_beat_task[0] == current_market.slug
                and official_price_to_beat_task[1].done()
            ):
                _, completed_task = official_price_to_beat_task
                official_price_to_beat_task = None
                try:
                    official_open = completed_task.result()
                except Exception as exc:
                    retry_delay = 3.0 if is_http_rate_limit(exc) else 1.0
                    official_price_to_beat_next_retry_monotonic = (
                        time.monotonic() + retry_delay
                    )
                    logger.warning(
                        "OFFICIAL_PRICE_TO_BEAT_RECONCILE_PENDING slug=%s "
                        "retry_in=%.1fs error=%s",
                        current_market.slug,
                        retry_delay,
                        exc,
                    )
                else:
                    difference = abs(
                        official_open.price_to_beat - provisional_open_price
                    )
                    official_price_to_beat_verified_slug = current_market.slug
                    start_price = official_open.price_to_beat
                    # Never copy legacy openPrice into the reversal boundary
                    # cache in TWAP mode. It can be incomplete and represents
                    # a different price series from Chainlink TWAP-60.
                    up_qty, down_qty = fast_hedge_engine.quantities()
                    has_paper_exposure = any(
                        position.slug == current_market.slug and not position.settled
                        for position in paper_positions
                    )
                    has_fast_hedge_exposure = (
                        args.strategy == "fast_directional_hedge_simple"
                        and fast_hedge_engine.state.market_slug == current_market.slug
                        and (up_qty > 0 or down_qty > 0)
                    )
                    has_exposure = (
                        signals_this_window > 0
                        or primary_orders_this_window > 0
                        or has_fast_hedge_exposure
                        or has_paper_exposure
                    )
                    provisional_open_mismatch_with_exposure = (
                        difference > max_price_alignment_difference
                        and has_exposure
                        and args.strategy not in REVERSAL_STRATEGIES
                        and args.strategy != "fast_directional_hedge_simple"
                    )
                    if difference <= max_price_alignment_difference:
                        logger.info(
                            "OFFICIAL_PRICE_TO_BEAT_RECONCILED slug=%s provisional=%s "
                            "official=%s difference=%s endpoint_incomplete=%s",
                            current_market.slug,
                            provisional_open_price,
                            start_price,
                            difference,
                            official_open.incomplete,
                        )
                    else:
                        prices = [spot.price]
                        price_sample_times = [time.monotonic()]
                        volatility_prices = [underlying_spot.price]
                        volatility_sample_times = [time.monotonic()]
                        underlying_start_price = start_price
                        confirmation_state.reset()
                        logger.error(
                            "OFFICIAL_PRICE_TO_BEAT_MISMATCH slug=%s provisional=%s "
                            "official=%s difference=%s max_difference=%s exposure=%s; "
                            "%s",
                            current_market.slug,
                            provisional_open_price,
                            start_price,
                            difference,
                            max_price_alignment_difference,
                            has_exposure,
                            (
                                "new orders blocked for the rest of this window"
                                if provisional_open_mismatch_with_exposure
                                else "samples reset to the official strike; reversal channel remains independent"
                            ),
                        )
                    if args.price_alignment_jsonl:
                        reconciliation_path = Path(args.price_alignment_jsonl)
                        reconciliation_path.parent.mkdir(parents=True, exist_ok=True)
                        with reconciliation_path.open("a", encoding="utf-8") as handle:
                            handle.write(
                                json.dumps(
                                    {
                                        "observed_at": datetime.now(timezone.utc).isoformat(),
                                        "slug": current_market.slug,
                                        "status": (
                                            "OFFICIAL_RECONCILED"
                                            if difference <= max_price_alignment_difference
                                            else "OFFICIAL_MISMATCH"
                                        ),
                                        "boundary_source": "official_price_to_beat_background_reconcile",
                                        "provisional_price_to_beat": str(provisional_open_price),
                                        "official_price_to_beat": str(start_price),
                                        "alignment_difference": str(difference),
                                        "endpoint_timestamp_ms": official_open.timestamp_ms,
                                        "endpoint_incomplete": official_open.incomplete,
                                    },
                                    ensure_ascii=True,
                                )
                                + "\n"
                            )
            if (
                official_price_to_beat_task is None
                and official_price_to_beat_verified_slug != current_market.slug
                and time.monotonic()
                >= official_price_to_beat_next_retry_monotonic
            ):
                official_price_to_beat_task = (
                    current_market.slug,
                    price_to_beat_executor.submit(
                        price_to_beat_client.fetch,
                        current_market.event_start_time,
                        current_market.end_time,
                    ),
                )
                logger.info(
                    "OFFICIAL_PRICE_TO_BEAT_RECONCILE_RETRY_STARTED slug=%s provisional=%s",
                    current_market.slug,
                    provisional_open_price,
                )

        if provisional_open_mismatch_with_exposure:
            sleep_until_next_poll(poll_interval, iteration_started_at)
            continue

        if (
            args.strategy in REVERSAL_STRATEGIES
            and reversal_runtime is not None
            and reversal_boundary_seeded_slug != current_market.slug
        ):
            if (
                reversal_pause_slug == current_market.slug
                and reversal_forced_exit_slug != current_market.slug
            ):
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            if notifications.trading_paused:
                logger.warning(
                    "REVERSAL_PAUSED slug=%s; Chainlink result processing and trading are paused",
                    current_market.slug,
                )
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            # The TWAP message that closes the previous window is also the
            # opening boundary for the current one. It can arrive just after
            # the first loop iteration, so retry here instead of disabling the
            # reversal channel for the entire five-minute window.
            if (
                current_resolution_mode is CryptoResolutionMode.TWAP_60
                and current_market.slug
                not in reversal_runtime.strategy.state.chainlink_open_prices
            ):
                boundary_timestamp_ms = int(
                    current_market.event_start_time.timestamp() * 1000
                )
                try:
                    retry_twap_boundary = price_client.polymarket_chainlink_twap_near(
                        boundary_timestamp_ms,
                        args.max_boundary_sample_offset_ms,
                    )
                except Exception as exc:
                    logger.info(
                        "REVERSAL_TWAP_BOUNDARY_RETRY_PENDING slug=%s "
                        "boundary_ms=%s error=%s",
                        current_market.slug,
                        boundary_timestamp_ms,
                        exc,
                    )
                else:
                    reversal_runtime.record_chainlink_open_price(
                        current_market.slug,
                        retry_twap_boundary.price,
                        allow_correction=True,
                    )
                    logger.info(
                        "REVERSAL_TWAP_BOUNDARY_RETRY_READY slug=%s "
                        "boundary=%s offset_ms=%s",
                        current_market.slug,
                        retry_twap_boundary.price,
                        retry_twap_boundary.observed_at_ms - boundary_timestamp_ms,
                    )
            official_boundary_pending = (
                current_resolution_mode is CryptoResolutionMode.TWAP_60
                and current_market.slug
                not in reversal_runtime.strategy.state.chainlink_open_prices
            )
            if official_boundary_pending:
                logger.info(
                    "REVERSAL_TWAP_BOUNDARY_PENDING slug=%s; same-source "
                    "Chainlink 60-second TWAP boundary is required",
                    current_market.slug,
                )
                if args.strategy != "reversal_or_fair_value":
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                boundary_outcomes = []
            else:
                boundary_outcomes = None
            reversal_result_source: str | None = None
            try:
                if boundary_outcomes is not None:
                    pass
                elif current_resolution_mode is CryptoResolutionMode.TWAP_60:
                    current_twap_boundary = (
                        reversal_runtime.strategy.state.chainlink_open_prices.get(
                            current_market.slug
                        )
                    )
                    if current_twap_boundary is None:
                        raise RuntimeError(
                            f"current Chainlink TWAP boundary unavailable for "
                            f"{current_market.slug}"
                        )
                    try:
                        completed_prices = fetch_reversal_twap_completed_prices(
                            current_market,
                            reversal_runtime.strategy.state.chainlink_open_prices,
                            current_twap_boundary,
                        )
                    except AmbiguousTwapResult as ambiguous:
                        consensus_result = reversal_terminal_book_results.get(
                            ambiguous.slug
                        )
                        consensus_source = "terminal_executable_book"
                        if (
                            consensus_result is None
                            and reversal_runtime.strategy.state.active_round is not None
                        ):
                            consensus_result = fetch_near_certain_market_winner(
                                ambiguous.slug
                            )
                            consensus_source = "gamma_near_certain_market_price"
                        if consensus_result is None:
                            raise
                        boundary_outcomes = (
                            reversal_runtime.observe_completed_window_results(
                                {ambiguous.slug: consensus_result}
                            )
                        )
                        reversal_result_source = consensus_source
                        logger.warning(
                            "REVERSAL_AMBIGUOUS_TWAP_PRICE_FALLBACK slug=%s "
                            "move=%s result=%s source=%s",
                            ambiguous.slug,
                            ambiguous.move,
                            consensus_result.value,
                            consensus_source,
                        )
                    else:
                        boundary_outcomes = (
                            reversal_runtime.observe_completed_window_prices(
                                completed_prices
                            )
                        )
                        reversal_result_source = (
                            "fixed_twap60_price_to_beat_vs_ending_twap"
                        )
                else:
                    chainlink_open_prices = fetch_reversal_chainlink_open_prices(
                        price_to_beat_client,
                        current_market,
                        start_price,
                        reversal_runtime.strategy.state.chainlink_open_prices,
                        reversal_runtime.strategy.settings.trigger_streak,
                    )
                    boundary_outcomes = reversal_runtime.observe_chainlink_open_prices(
                        chainlink_open_prices
                    )
            except Exception as exc:
                if not suppress_reversal_boundary_alert(exc):
                    notifications.notify_exception(
                        f"反转策略 Chainlink 边界结果 {current_market.slug}",
                        exc,
                        key=f"reversal-boundary:{current_market.slug}",
                    )
                logger.warning(
                    "REVERSAL_CHAINLINK_PENDING slug=%s rate_limited=%s "
                    "ambiguous_twap=%s notification_suppressed=%s error=%s; "
                    "%s",
                    current_market.slug,
                    is_http_rate_limit(exc),
                    isinstance(exc, AmbiguousTwapResult),
                    suppress_reversal_boundary_alert(exc),
                    exc,
                    (
                        "reversal channel waits while the independent fair-value channel continues"
                        if args.strategy == "reversal_or_fair_value"
                        else "no split submitted and retry follows"
                    ),
                )
                if args.strategy != "reversal_or_fair_value":
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                boundary_outcomes = []
            else:
                if not official_boundary_pending:
                    reversal_boundary_seeded_slug = current_market.slug
                    for result_slug, settlement_status in boundary_outcomes:
                        result = reversal_runtime.strategy.state.pending_gamma_results.get(
                            result_slug
                        )
                        logger.info(
                            "REVERSAL_CHAINLINK_RESULT slug=%s result=%s status=%s source=%s",
                            result_slug,
                            result.value if result is not None else "already_settled",
                            settlement_status,
                            (
                                reversal_result_source
                                if current_resolution_mode is CryptoResolutionMode.TWAP_60
                                else "official_open_boundary"
                            ),
                        )

        rolling_volatility_prices, rolling_volatility_sample_times = (
            cross_window_volatility_series(btc_volatility_samples)
        )
        sigma = estimate_sigma_per_sqrt_second(
            rolling_volatility_prices,
            Decimal(str(poll_interval)),
            fallback_sigma,
            rolling_volatility_sample_times,
        )
        remaining_seconds = max(Decimal("0"), seconds_to_end)
        known_twap_overlap = (
            trailing_time_weighted_average(
                volatility_prices,
                volatility_sample_times,
                Decimal("60") - remaining_seconds,
            )
            if (
                current_resolution_mode is CryptoResolutionMode.TWAP_60
                and Decimal("0") < remaining_seconds < Decimal("60")
            )
            else None
        )
        fair = (
            btc_up_twap_probability(
                start_price,
                spot.price,
                underlying_spot.price,
                remaining_seconds,
                sigma,
                known_overlap_average=known_twap_overlap,
            )
            if current_resolution_mode is CryptoResolutionMode.TWAP_60
            else btc_up_probability(
                start_price,
                spot.price,
                remaining_seconds,
                sigma,
            )
        )
        up_quote, down_quote = quote_outcomes(clob, current_market)
        up_ask = up_quote.ask if up_quote else None
        down_ask = down_quote.ask if down_quote else None
        if up_ask is not None and down_ask is not None:
            up_ask_prices.append(up_ask)
            down_ask_prices.append(down_ask)
            book_sample_times.append(time.monotonic())
        if (
            args.strategy in REVERSAL_STRATEGIES
            and current_resolution_mode is CryptoResolutionMode.TWAP_60
            and 0 <= seconds_to_end <= 2
        ):
            terminal_winner = terminal_book_winner(up_quote, down_quote)
            previous_terminal_winner = reversal_terminal_book_results.get(
                current_market.slug
            )
            if terminal_winner is None:
                reversal_terminal_book_results.pop(current_market.slug, None)
            else:
                reversal_terminal_book_results[current_market.slug] = terminal_winner
                if terminal_winner is not previous_terminal_winner:
                    logger.info(
                        "REVERSAL_TERMINAL_BOOK_READY slug=%s result=%s "
                        "up_bid=%s down_bid=%s seconds_left=%.3f",
                        current_market.slug,
                        terminal_winner.value,
                        up_quote.bid if up_quote is not None else None,
                        down_quote.bid if down_quote is not None else None,
                        seconds_to_end,
                    )
            ordered_terminal_slugs = sorted(
                reversal_terminal_book_results,
                key=lambda value: int(value.rpartition("-")[2]),
            )
            for expired_terminal_slug in ordered_terminal_slugs[:-6]:
                reversal_terminal_book_results.pop(expired_terminal_slug, None)
        action = choose_theoretical_action(fair.probability_up, up_ask, down_ask, edge_threshold)

        if args.strategy == "reversal_four_64" and args.paper_trading:
            paper_bankroll = scratch_decayed_paper_positions(
                paper_positions,
                paper_bankroll,
                current_market.slug,
                shrink_probability_toward_even(
                    fair.probability_up,
                    probability_shrinkage,
                ),
                up_quote,
                down_quote,
                fair_scratch_exit_probability,
                fair_scratch_price_tolerance,
                fair_scratch_fee_rate,
            )

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
            "%s seconds_left=%s settlement_price=%s underlying_spot=%s start=%s "
            "fair_model=%s p_up=%.4f sigma=%.8f up=%s down=%s fair_action=%s",
            current_market.slug,
            int(seconds_to_end),
            spot.price,
            underlying_spot.price,
            start_price,
            (
                "twap60_overlap_integral"
                if current_resolution_mode is CryptoResolutionMode.TWAP_60
                else "terminal_spot"
            ),
            float(fair.probability_up),
            float(fair.sigma_per_sqrt_second),
            up_quote,
            down_quote,
            action,
        )

        hybrid_fair_value_fallback = False
        if (
            args.strategy in REVERSAL_STRATEGIES
            and reversal_runtime is not None
            and reversal_boundary_seeded_slug != current_market.slug
        ):
            if reversal_runtime.strategy.state.active_round is not None:
                logger.warning(
                    "REVERSAL_RESULT_UNCONFIRMED_PAUSE slug=%s; active round "
                    "cannot advance until the previous result is final",
                    current_market.slug,
                )
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            if (
                args.strategy == "reversal_or_fair_value"
                and hybrid_signal_owner is None
            ):
                hybrid_signal_owner = "fair_value"
                logger.info(
                    "REVERSAL_RESULT_UNCONFIRMED_FAIR_VALUE_ONLY slug=%s; "
                    "reversal channel is disabled for this window",
                    current_market.slug,
                )
        if args.strategy in REVERSAL_STRATEGIES and not (
            args.strategy == "reversal_or_fair_value"
            and hybrid_signal_owner == "fair_value"
        ):
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
            if notifications.trading_paused:
                logger.warning(
                    "REVERSAL_PAUSED slug=%s; no chain transaction or order submitted",
                    current_market.slug,
                )
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            if (
                reversal_pause_slug == current_market.slug
                and reversal_forced_exit_slug != current_market.slug
            ):
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            try:
                stale_reconciliation = reversal_runtime.reconcile_stale_active_round()
                if stale_reconciliation is not None:
                    stale_slug, stale_status = stale_reconciliation
                    logger.warning(
                        "REVERSAL_STALE_ACTIVE_RECONCILED slug=%s status=%s; "
                        "missed windows were not replayed",
                        stale_slug,
                        stale_status,
                    )
                abandoned_slug = (
                    reversal_runtime.strategy.roll_forward_uncertain_opening(
                        current_market.slug
                    )
                )
                if abandoned_slug is not None:
                    reversal_runtime.save()
                    logger.warning(
                        "REVERSAL_UNCERTAIN_ROLLED_FORWARD old_slug=%s current_slug=%s; "
                        "new-window trading may resume",
                        abandoned_slug,
                        current_market.slug,
                    )
                next_amount = reversal_runtime.strategy.opening_split_amount(
                    current_market.slug
                )
                up_book, down_book = clob.books(current_market.token_ids)
                if reversal_forced_exit_slug == current_market.slug:
                    correction_result = reversal_runtime.correct_gamma_mismatch_position(
                        market=current_market,
                        up_book=up_book,
                        down_book=down_book,
                        seconds_left=Decimal(str(max(0.0, seconds_to_end))),
                        source="chain",
                        allow_replacement=False,
                    )
                    logger.warning(
                        "REVERSAL_CHAIN_FORCED_EXIT_RETRY slug=%s status=%s detail=%s",
                        current_market.slug,
                        correction_result.status,
                        correction_result.detail,
                    )
                    if correction_result.order is not None:
                        live_summary["orders"].append(correction_result.order)
                        live_orders_submitted += 1
                        live_summary["order_attempts"] = live_orders_submitted
                        write_live_summary(finalize=False)
                    if reversal_runtime.strategy.state.active_round is None:
                        reversal_forced_exit_slug = None
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                reversal_open_price = (
                    reversal_runtime.strategy.state.chainlink_open_prices.get(
                        current_market.slug,
                        start_price,
                    )
                    if current_resolution_mode is CryptoResolutionMode.TWAP_60
                    else start_price
                )
                def reversal_health_for_books(
                    latest_up_book: OrderBookSnapshot,
                    latest_down_book: OrderBookSnapshot,
                ) -> dict[Direction, MarketHealth]:
                    return {
                        side: market_health_from_books(
                            trend_side=side,
                            up_book=latest_up_book,
                            down_book=latest_down_book,
                            making_amount=next_amount,
                            spot_prices=prices,
                            open_price=reversal_open_price,
                            short_volatility_override=(
                                reversal_entry_rv60
                                if reversal_entry_rv60 is not None
                                else Decimal("0")
                            ),
                            five_minute_volatility_override=(
                                reversal_entry_rv300
                                if reversal_entry_rv300 is not None
                                else Decimal("0")
                            ),
                        )
                        for side in (Direction.UP, Direction.DOWN)
                    }

                reversal_health_by_side = reversal_health_for_books(
                    up_book,
                    down_book,
                )
                reversal_result = reversal_runtime.tick(
                    market=current_market,
                    up_book=up_book,
                    down_book=down_book,
                    health_by_side=reversal_health_by_side,
                    book_refresh=lambda: clob.books(current_market.token_ids),
                    spot_price=spot.price,
                    open_price=reversal_open_price,
                    probability_up=(
                        None
                        if args.strategy == "reversal_or_fair_value"
                        else fair.probability_up
                    ),
                )
                logger.info(
                    "REVERSAL_V11 slug=%s status=%s plan=%s detail=%s",
                    current_market.slug,
                    reversal_result.status,
                    reversal_result.plan,
                    reversal_result.detail,
                )
                while (
                    reversal_result.status == "entry_unmatched"
                    and reversal_result.plan is not None
                    and reversal_runtime.strategy.settings.uses_first_stage_order_rules(
                        reversal_result.plan.attempt
                    )
                    and reversal_runtime.strategy.state.active_round is not None
                ):
                    # Retry against a newly fetched book while the opening
                    # liquidity is still present. The runtime re-applies the
                    # spread, depth and 0.64 ask ceiling before every FAK.
                    time.sleep(REVERSAL_FAST_FAK_RETRY_DELAY_SECONDS)
                    up_book, down_book = clob.books(current_market.token_ids)
                    reversal_result = reversal_runtime.tick(
                        market=current_market,
                        up_book=up_book,
                        down_book=down_book,
                        health_by_side=reversal_health_for_books(
                            up_book,
                            down_book,
                        ),
                        spot_price=spot.price,
                        open_price=reversal_open_price,
                        probability_up=(
                            None
                            if args.strategy == "reversal_or_fair_value"
                            else fair.probability_up
                        ),
                    )
                    logger.info(
                        "REVERSAL_V11_FAST_RETRY slug=%s status=%s plan=%s detail=%s",
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
                        if args.strategy == "reversal_or_fair_value":
                            hybrid_signal_owner = "reversal"
                        live_orders_matched += 1
                        live_summary["matched_orders"] = live_orders_matched
                        live_summary["error"] = None
                    write_live_summary(finalize=False)
                for audit_source, verified_slug, verified_result in (
                    reversal_runtime.pump_historical_audits(
                        max_candidates=2,
                        worker_time_budget_seconds=2.0,
                        apply_time_budget_seconds=0.01,
                    )
                ):
                    logger.info(
                        "REVERSAL_HISTORY_VERIFIED source=%s slug=%s result=%s "
                        "phase=after_current_window_action",
                        audit_source,
                        verified_slug,
                        verified_result,
                    )
                if maintenance_ready and reversal_notifications_may_run(
                    reversal_result.status
                ):
                    run_slow_notification_maintenance(maintenance_slug)
                maybe_execute_weekly_restart()
                if args.strategy == "reversal_or_fair_value":
                    hybrid_fair_value_fallback = hybrid_fair_value_fallback_allowed(
                        args.strategy,
                        reversal_result.order,
                        reversal_runtime.strategy.state.active_round,
                        reversal_runtime.strategy.state.prepared_split,
                        current_slug=current_market.slug,
                        signal_owner=hybrid_signal_owner,
                    )
            except Exception as exc:
                live_summary["error"] = f"{type(exc).__name__}: {exc}"
                if is_reversal_clob_timeout(exc):
                    # A transient read/connection timeout cannot authorize a
                    # new order. The current pass fails closed; if submission
                    # state was uncertain, the runtime reconciles balances on
                    # the next pass before it can retry. Keep the diagnostic in
                    # local logs without sending a noisy Telegram alert.
                    logger.warning(
                        "REVERSAL_CLOB_TIMEOUT_NOTIFICATION_SUPPRESSED "
                        "slug=%s error=%s",
                        current_market.slug,
                        exc,
                    )
                else:
                    notifications.notify_exception(
                        f"反转策略 {current_market.slug}",
                        exc,
                        key=f"reversal:{current_market.slug}",
                        cooldown=300,
                    )
                active_reversal = reversal_runtime.strategy.state.active_round
                prepared_reversal = reversal_runtime.strategy.state.prepared_split
                if isinstance(exc, ChainResultMismatch):
                    reversal_runtime.quarantine_chain_mismatch(exc)
                    reversal_pause_slug = current_market.slug
                    reversal_forced_exit_slug = current_market.slug
                    try:
                        correction_result = reversal_runtime.correct_gamma_mismatch_position(
                            market=current_market,
                            up_book=up_book,
                            down_book=down_book,
                            seconds_left=Decimal(str(max(0.0, seconds_to_end))),
                            source="chain",
                            allow_replacement=False,
                        )
                        logger.warning(
                            "REVERSAL_CHAIN_CORRECTION slug=%s result_slug=%s "
                            "status=%s detail=%s",
                            current_market.slug,
                            exc.slug,
                            correction_result.status,
                            correction_result.detail,
                        )
                        if correction_result.order is not None:
                            live_summary["orders"].append(correction_result.order)
                            live_orders_submitted += 1
                            live_summary["order_attempts"] = live_orders_submitted
                            write_live_summary(finalize=False)
                        if reversal_runtime.strategy.state.active_round is None:
                            reversal_forced_exit_slug = None
                    except Exception as correction_exc:
                        logger.exception(
                            "REVERSAL_CHAIN_CORRECTION_FAILED slug=%s result_slug=%s "
                            "error=%s; forced-exit retry remains armed",
                            current_market.slug,
                            exc.slug,
                            correction_exc,
                        )
                elif isinstance(exc, GammaResultMismatch):
                    reversal_runtime.quarantine_gamma_mismatch(exc)
                    reversal_pause_slug = current_market.slug
                    logger.error(
                        "REVERSAL_GAMMA_AUDIT_CONFLICT slug=%s result_slug=%s; "
                        "current window paused without position mutation",
                        current_market.slug,
                        exc.slug,
                    )
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
                    reversal_pause_slug = current_market.slug
                    logger.warning(
                        "REVERSAL_WINDOW_PAUSE_SET slug=%s; global trading remains enabled",
                        current_market.slug,
                    )
        elif (
            args.strategy == "reversal_or_fair_value"
            and hybrid_signal_owner == "fair_value"
        ):
            hybrid_fair_value_fallback = True
            logger.info(
                "HYBRID_FAIR_VALUE_OWNER slug=%s; reversal channel is locked out",
                current_market.slug,
            )

        if args.strategy in REVERSAL_STRATEGIES:
            if not hybrid_fair_value_fallback:
                sleep_until_next_poll(poll_interval, iteration_started_at)
                continue
            logger.info(
                "HYBRID_FAIR_VALUE_FALLBACK slug=%s reversal_status=%s",
                current_market.slug,
                (
                    reversal_result.status
                    if hybrid_signal_owner != "fair_value"
                    else "fair_value_owns_window"
                ),
            )

        if (
            args.auto_trade
            and (
                signals_this_window < strategy_trade_limit(args.strategy, args.max_trades)
                or (
                    args.strategy == "fast_directional_hedge_simple"
                    and fast_hedge_engine.state.active_trade is not None
                    and fast_hedge_engine.state.active_trade.status == "RISK_EXIT"
                )
            )
            and not risk_pause_active_for_window
            and not notifications.trading_paused
        ):
            if args.strategy == "fast_directional_hedge_simple":
                try:
                    fast_up_book, fast_down_book = clob.books(
                        current_market.token_ids
                    )
                except RequestException as exc:
                    logger.warning(
                        "FAST_DIRECTIONAL_HEDGE_BOOK_PENDING slug=%s error=%s",
                        current_market.slug,
                        exc,
                    )
                    signal = None
                else:
                    signal = choose_fast_directional_hedge_simple_signal(
                        fast_hedge_engine,
                        current_market,
                        start_price,
                        underlying_spot.price,
                        seconds_to_end,
                        fair.sigma_per_sqrt_second,
                        fair.probability_up,
                        volatility_prices,
                        volatility_sample_times,
                        fast_up_book,
                        fast_down_book,
                        time.time(),
                    )
            elif args.strategy == "late_favorite":
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
            elif args.strategy == "ewma_twap_fair":
                signal = (
                    choose_ewma_twap_signal(
                        current_market,
                        list(btc_volatility_samples),
                        underlying_spot.price,
                        start_price,
                        seconds_to_end,
                        up_quote,
                        down_quote,
                        ewma_twap_settings,
                        fallback_sigma,
                    )
                    if current_resolution_mode is CryptoResolutionMode.TWAP_60
                    else None
                )
            elif args.strategy == "momentum_confirmation":
                signal = choose_momentum_confirmation_signal(
                    current_market,
                    up_quote,
                    down_quote,
                    seconds_to_end,
                    volatility_prices,
                    volatility_sample_times,
                    underlying_start_price or Decimal("0"),
                    momentum_entry_seconds,
                    momentum_cutoff_seconds,
                    momentum_min_move_percent,
                    momentum_min_move_usd,
                    momentum_min_entry,
                    momentum_max_entry,
                    max_spread,
                    min_ask_sum,
                    max_ask_sum,
                    momentum_confirmation_seconds,
                )
            elif args.strategy == "reversal_four_64":
                signal = choose_fair_value_scratch_signal(
                    current_market,
                    fair.probability_up,
                    up_quote,
                    down_quote,
                    seconds_to_end,
                    fair_scratch_entry_seconds,
                    fair_scratch_cutoff_seconds,
                    fair_scratch_min_entry,
                    fair_scratch_max_entry,
                    fair_scratch_min_probability,
                    fair_scratch_min_net_edge,
                    fair_scratch_fee_rate,
                    max_spread,
                    min_ask_sum,
                    max_ask_sum,
                    probability_shrinkage,
                )
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
                    volatility_prices,
                    underlying_start_price or Decimal("0"),
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
                        up_ask_prices,
                        down_ask_prices,
                        book_sample_times,
                        fair_value_book_trend_samples,
                        fair_value_book_min_slope,
                        fair_value_book_min_relative_slope,
                        fair_value_book_max_pullback,
                        low_entry_cutoff,
                        low_entry_min_win_probability,
                        probability_shrinkage,
                        fair_value_fee_rate,
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
                if signal.reason.startswith((
                    "protective_open_reversal_stop",
                    "ewma_twap_fair",
                    "book_volatility_arbitrage_v2.0",
                    "fair_value_edge",
                )):
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
                        if args.strategy == "fast_directional_hedge_simple"
                        else fair_value_confirmation_min_seconds
                        if is_fair_value_strategy(args.strategy)
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
                if current_resolution_mode is CryptoResolutionMode.TWAP_60:
                    # The boundary strike is frozen for the order attempt. An
                    # official fetch runs independently and may reconcile later;
                    # never put that HTTP request back on the submit path.
                    logger.info(
                        "PRICE_TO_BEAT_PRE_SUBMIT_FROZEN slug=%s price=%s status=%s",
                        current_market.slug,
                        start_price,
                        (
                            "official_reconciled"
                            if official_price_to_beat_verified_slug
                            == current_market.slug
                            else "chainlink_provisional"
                        ),
                    )
                else:
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
                        volatility_prices = []
                        volatility_sample_times = []
                        underlying_start_price = None
                        up_ask_prices = []
                        down_ask_prices = []
                        book_sample_times = []
                        confirmation_state.reset()
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue

                confirmed_signal = signal
                try:
                    if args.strategy == "fast_directional_hedge_simple":
                        # Reuse the already-fetched same-loop Chainlink values.
                        # The V1.1 direction/invalidation gates therefore add
                        # no network request to the order hot path.
                        refreshed_spot = spot
                        refreshed_underlying_spot = underlying_spot
                    elif current_resolution_mode is CryptoResolutionMode.TWAP_60:
                        refreshed_spot = price_client.polymarket_chainlink_twap()
                        refreshed_underlying_spot = price_client.btc_usd()
                    else:
                        refreshed_spot = price_client.btc_usd()
                        refreshed_underlying_spot = refreshed_spot
                    if refreshed_spot.observed_at is not None:
                        refreshed_age = abs(int(time.time()) - refreshed_spot.observed_at)
                        if refreshed_age > args.max_spot_age:
                            raise RuntimeError(
                                f"Pre-submit Chainlink report is stale by {refreshed_age}s"
                            )
                    if refreshed_underlying_spot.observed_at is not None:
                        refreshed_underlying_age = abs(
                            int(time.time()) - refreshed_underlying_spot.observed_at
                        )
                        if refreshed_underlying_age > args.max_spot_age:
                            raise RuntimeError(
                                "Pre-submit underlying Chainlink report is stale by "
                                f"{refreshed_underlying_age}s"
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
                fast_simple_order = args.strategy == "fast_directional_hedge_simple"
                if refreshed_quote is None or refreshed_quote.ask is None:
                    logger.info(
                        "ORDER_BLOCKED_PRE_SUBMIT_QUOTE slug=%s side=%s reason=missing_ask",
                        current_market.slug,
                        confirmed_signal.side,
                    )
                    sleep_until_next_poll(poll_interval, iteration_started_at)
                    continue
                ask_drop = confirmed_signal.price - refreshed_quote.ask
                if not fast_simple_order and ask_drop > pre_submit_max_adverse_ask_drop:
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
                if not fast_simple_order and ask_worsening > pre_submit_max_ask_worsening:
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
                refreshed_volatility_prices = [
                    *volatility_prices,
                    refreshed_underlying_spot.price,
                ]
                refreshed_volatility_sample_times = [
                    *volatility_sample_times,
                    refreshed_at,
                ]
                refreshed_seconds_to_end = max(
                    Decimal("0"),
                    _seconds_to_end(current_market, datetime.now(timezone.utc)),
                )
                refreshed_rolling_prices, refreshed_rolling_times = (
                    cross_window_volatility_series(
                        btc_volatility_samples,
                        (time.time(), refreshed_underlying_spot.price),
                    )
                )
                refreshed_sigma = estimate_sigma_per_sqrt_second(
                    refreshed_rolling_prices,
                    Decimal(str(poll_interval)),
                    fallback_sigma,
                    refreshed_rolling_times,
                )
                refreshed_known_twap_overlap = (
                    trailing_time_weighted_average(
                        refreshed_volatility_prices,
                        refreshed_volatility_sample_times,
                        Decimal("60") - refreshed_seconds_to_end,
                    )
                    if (
                        current_resolution_mode is CryptoResolutionMode.TWAP_60
                        and Decimal("0")
                        < refreshed_seconds_to_end
                        < Decimal("60")
                    )
                    else None
                )
                refreshed_fair = (
                    btc_up_twap_probability(
                        start_price,
                        refreshed_spot.price,
                        refreshed_underlying_spot.price,
                        refreshed_seconds_to_end,
                        refreshed_sigma,
                        known_overlap_average=refreshed_known_twap_overlap,
                    )
                    if current_resolution_mode is CryptoResolutionMode.TWAP_60
                    else btc_up_probability(
                        start_price,
                        refreshed_spot.price,
                        refreshed_seconds_to_end,
                        refreshed_sigma,
                    )
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
                elif args.strategy == "fast_directional_hedge_simple":
                    refreshed_signal = choose_fast_directional_hedge_simple_signal(
                        fast_hedge_engine,
                        current_market,
                        start_price,
                        refreshed_underlying_spot.price,
                        refreshed_seconds_to_end,
                        refreshed_fair.sigma_per_sqrt_second,
                        refreshed_fair.probability_up,
                        refreshed_volatility_prices,
                        refreshed_volatility_sample_times,
                        refreshed_up_book,
                        refreshed_down_book,
                        time.time(),
                    )
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
                elif args.strategy == "ewma_twap_fair":
                    refreshed_signal = (
                        choose_ewma_twap_signal(
                            current_market,
                            list(
                                zip(
                                    refreshed_rolling_times,
                                    refreshed_rolling_prices,
                                )
                            ),
                            refreshed_underlying_spot.price,
                            start_price,
                            refreshed_seconds_to_end,
                            refreshed_up_quote,
                            refreshed_down_quote,
                            ewma_twap_settings,
                            fallback_sigma,
                        )
                        if current_resolution_mode is CryptoResolutionMode.TWAP_60
                        else None
                    )
                elif args.strategy == "momentum_confirmation":
                    refreshed_signal = choose_momentum_confirmation_signal(
                        current_market,
                        refreshed_up_quote,
                        refreshed_down_quote,
                        refreshed_seconds_to_end,
                        refreshed_volatility_prices,
                        refreshed_volatility_sample_times,
                        underlying_start_price or Decimal("0"),
                        momentum_entry_seconds,
                        momentum_cutoff_seconds,
                        momentum_min_move_percent,
                        momentum_min_move_usd,
                        momentum_min_entry,
                        momentum_max_entry,
                        max_spread,
                        min_ask_sum,
                        max_ask_sum,
                        momentum_confirmation_seconds,
                    )
                elif args.strategy == "reversal_four_64":
                    refreshed_signal = choose_fair_value_scratch_signal(
                        current_market,
                        refreshed_fair.probability_up,
                        refreshed_up_quote,
                        refreshed_down_quote,
                        refreshed_seconds_to_end,
                        fair_scratch_entry_seconds,
                        fair_scratch_cutoff_seconds,
                        fair_scratch_min_entry,
                        fair_scratch_max_entry,
                        fair_scratch_min_probability,
                        fair_scratch_min_net_edge,
                        fair_scratch_fee_rate,
                        max_spread,
                        min_ask_sum,
                        max_ask_sum,
                        probability_shrinkage,
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
                        refreshed_volatility_prices,
                        underlying_start_price or Decimal("0"),
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
                        [
                            *up_ask_prices[:-1],
                            *(
                                [refreshed_up_quote.ask]
                                if refreshed_up_quote is not None
                                and refreshed_up_quote.ask is not None
                                else []
                            ),
                        ],
                        [
                            *down_ask_prices[:-1],
                            *(
                                [refreshed_down_quote.ask]
                                if refreshed_down_quote is not None
                                and refreshed_down_quote.ask is not None
                                else []
                            ),
                        ],
                        [*book_sample_times[:-1], refreshed_at],
                        fair_value_book_trend_samples,
                        fair_value_book_min_slope,
                        fair_value_book_min_relative_slope,
                        fair_value_book_max_pullback,
                        low_entry_cutoff,
                        low_entry_min_win_probability,
                        probability_shrinkage,
                        fair_value_fee_rate,
                    )

                if (
                    refreshed_signal is None
                    or refreshed_signal.side != confirmed_signal.side
                    or refreshed_signal.action != confirmed_signal.action
                    or refreshed_signal.role != confirmed_signal.role
                ):
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
                volatility_prices = refreshed_volatility_prices
                volatility_sample_times = refreshed_volatility_sample_times
                fair = refreshed_fair
                up_quote, down_quote = refreshed_up_quote, refreshed_down_quote
                seconds_to_end = refreshed_seconds_to_end
                trade_size = signal.size if signal.size is not None else order_size
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
                        else signal.price
                        if args.strategy == "fast_directional_hedge_simple"
                        else max_entry + live_buy_slippage
                    )
                    if args.strategy in {"fast_directional_hedge_simple", "ewma_twap_fair"}:
                        execution_price = signal.price
                    elif signal.reason.startswith("one_way_trend"):
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
                            fair_value_fee_rate,
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
                        size=signal.size,
                        fair_probability=signal.fair_probability,
                        action=signal.action,
                        role=signal.role,
                        executable_price=signal.executable_price,
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
                        size=signal.size,
                        fair_probability=signal.fair_probability,
                        action=signal.action,
                        role=signal.role,
                        executable_price=signal.executable_price,
                    )
                if trader is not None:
                    selected_book = (
                        refreshed_up_book
                        if signal.side == "UP"
                        else refreshed_down_book
                    )
                    available_depth = (
                        executable_bid_depth(selected_book, signal.price)
                        if signal.action == "SELL"
                        else executable_ask_depth(selected_book, signal.price)
                    )
                    if available_depth < trade_size and not (
                        args.strategy == "fast_directional_hedge_simple"
                        and available_depth > 0
                    ):
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
                        size=signal.size,
                        fair_probability=signal.fair_probability,
                        action=signal.action,
                        role=signal.role,
                        executable_price=signal.executable_price,
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
                    if not (
                        args.strategy == "fast_directional_hedge_simple"
                        and signal.role in {"HEDGE", "EXIT"}
                    ):
                        signals_this_window = window_trade_count_after_attempt(
                            signals_this_window,
                            live=False,
                    )
                    if args.paper_trading:
                        paper_execution_price = signal.executable_price or signal.price
                        paper_trade_stake = (
                            paper_execution_price * trade_size
                            if args.strategy in {"fast_directional_hedge_simple", "ewma_twap_fair"}
                            else signal.price * paper_shares
                            if paper_shares > 0
                            else paper_stake
                        )
                        paper_fee_rate = (
                            late_fee_rate
                            if args.strategy == "late_favorite"
                            else fair_scratch_fee_rate
                            if args.strategy == "reversal_four_64"
                            else momentum_fee_rate
                            if args.strategy == "momentum_confirmation"
                            else smart_score_fee_rate
                            if args.strategy == "smart_score"
                            else ewma_twap_settings.taker_fee_rate
                            if args.strategy == "ewma_twap_fair"
                            else fast_hedge_settings.fee_rate
                            if args.strategy == "fast_directional_hedge_simple"
                            else Decimal("0")
                        )
                        paper_position_count_before = len(paper_positions)
                        if (
                            args.strategy == "fast_directional_hedge_simple"
                            and signal.action == "SELL"
                        ):
                            paper_bankroll, exit_shares, exit_proceeds = (
                                close_fast_simple_paper_position(
                                    paper_positions,
                                    paper_bankroll,
                                    current_market.slug,
                                    signal.side,
                                    trade_size,
                                    paper_execution_price,
                                    paper_fee_rate,
                                )
                            )
                            if exit_shares > 0:
                                fast_hedge_engine.record_exit_fill(
                                    current_market.slug,
                                    signal.side,
                                    exit_shares,
                                    exit_proceeds,
                                )
                        else:
                            paper_bankroll = open_paper_position(
                                paper_positions,
                                paper_bankroll,
                                current_market.slug,
                                replace(signal, price=paper_execution_price),
                                paper_trade_stake,
                                paper_fee_rate,
                            )
                            if (
                                args.strategy == "fast_directional_hedge_simple"
                                and len(paper_positions) > paper_position_count_before
                            ):
                                fast_hedge_engine.record_fill(
                                    current_market.slug,
                                    signal.side,
                                    trade_size,
                                    paper_trade_stake,
                                )
                        if args.stop_when_bust and paper_bankroll <= 0:
                            logger.info("PAPER_BUST bankroll=%s. Exiting after open position.", paper_bankroll)
                            return
                    else:
                        logger.info("DRY RUN: would buy %s at %s x %s", signal.side, signal.price, trade_size)
                    if is_protective_hedge:
                        aggregate_protection_completed = True
                        one_way_reversal_started_at = None
                    elif primary_side_this_window is None and not (
                        args.strategy == "fast_directional_hedge_simple"
                        and signal.role in {"HEDGE", "EXIT"}
                    ):
                        if args.strategy == "reversal_or_fair_value":
                            hybrid_signal_owner = "fair_value"
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
                    fast_simple_role = (
                        signal.role
                        if args.strategy == "fast_directional_hedge_simple"
                        else "ENTRY"
                    )
                    fast_simple_risk_order = fast_simple_role in {"HEDGE", "EXIT"}
                    live_notional_cap = (
                        notional
                        if fast_simple_risk_order
                        else
                        protection_notional
                        if is_protective_hedge
                        else ewma_twap_settings.max_notional
                        if args.strategy == "ewma_twap_fair"
                        else late_max_live_notional
                        if args.strategy == "late_favorite"
                        else max_live_notional
                    )
                    if (
                        not fast_simple_risk_order
                        and not live_session_should_continue(live_orders_submitted, args.max_live_orders)
                    ):
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
                        "action": signal.action,
                        "token_id": signal.token_id,
                        "price": str(signal.price),
                        "size": str(trade_size),
                        "notional": str(notional),
                        "order_type": (
                            "FAK"
                            if args.strategy == "fast_directional_hedge_simple"
                            else args.live_order_type
                        ),
                        "order_role": (
                            "stop_hedge"
                            if args.strategy == "fast_directional_hedge_simple"
                            and fast_simple_role == "HEDGE"
                            else "stop_exit"
                            if args.strategy == "fast_directional_hedge_simple"
                            and fast_simple_role == "EXIT"
                            else "directional_entry"
                            if args.strategy == "fast_directional_hedge_simple"
                            else "reverse_protection"
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
                        if args.strategy == "fast_directional_hedge_simple":
                            fast_hedge_engine.mark_submission_started(fast_simple_role)
                        submission_order_type = (
                            "FAK"
                            if args.strategy == "fast_directional_hedge_simple"
                            else args.live_order_type
                        )
                        order_method = (
                            trader.sell_limit
                            if signal.action == "SELL"
                            else trader.buy_limit
                        )
                        response = order_method(
                            token_id=signal.token_id,
                            price=signal.price,
                            size=trade_size,
                            tick_size=current_market.minimum_tick_size,
                            neg_risk=current_market.neg_risk,
                            order_type=submission_order_type,
                            submit_not_after_monotonic=submit_not_after_monotonic,
                        )
                    except OrderQuoteExpiredError as exc:
                        if args.strategy == "fast_directional_hedge_simple":
                            fast_hedge_engine.mark_submission_failed(uncertain=False)
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
                        if args.strategy == "fast_directional_hedge_simple":
                            fast_hedge_engine.mark_submission_failed(uncertain=True)
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
                        if not fast_simple_risk_order and not live_session_should_continue(live_orders_submitted, args.max_live_orders):
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
                        require_fill_amounts=(
                            args.strategy == "fast_directional_hedge_simple"
                            or args.live_order_type == "FAK"
                        ),
                    )
                    if not fast_simple_risk_order:
                        signals_this_window = window_trade_count_after_attempt(
                            signals_this_window,
                            live=True,
                            matched=matched,
                        )
                    if not matched:
                        if args.strategy == "fast_directional_hedge_simple":
                            fast_hedge_engine.mark_submission_failed(uncertain=False)
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
                        if not fast_simple_risk_order and not live_session_should_continue(live_orders_submitted, args.max_live_orders):
                            live_summary["status"] = "completed"
                            write_live_summary()
                            return
                        sleep_until_next_poll(poll_interval, iteration_started_at)
                        continue
                    live_orders_matched += 1
                    if args.strategy == "fast_directional_hedge_simple":
                        if signal.action == "SELL":
                            fast_fill_proceeds, fast_fill_shares = response_sell_fill_amounts(
                                response,
                                signal.price,
                                trade_size,
                            )
                            fast_hedge_engine.record_exit_fill(
                                current_market.slug,
                                signal.side,
                                fast_fill_shares,
                                fast_fill_proceeds,
                            )
                        else:
                            fast_fill_cost, fast_fill_shares = response_fill_amounts(
                                response,
                                signal.price,
                                trade_size,
                            )
                            fast_hedge_engine.record_fill(
                                current_market.slug,
                                signal.side,
                                fast_fill_shares,
                                fast_fill_cost,
                            )
                    if is_protective_hedge:
                        aggregate_protection_completed = True
                        one_way_reversal_started_at = None
                    elif primary_side_this_window is None and not fast_simple_risk_order:
                        if args.strategy == "reversal_or_fair_value":
                            hybrid_signal_owner = "fair_value"
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
                    if not fast_simple_risk_order and not live_session_should_continue(live_orders_submitted, args.max_live_orders):
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
        if args.strategy == "fast_directional_hedge_simple":
            record_fast_simple_paper_settlements(fast_hedge_engine, paper_positions)
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
