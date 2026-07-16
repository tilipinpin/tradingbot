from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import re
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
from src.polymarket import ClobDataClient, ClobTradingClient, GammaClient, Market, OrderBookQuote
from src.price_signal import SpotPriceClient
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
    settled: bool = False
    profit: Decimal | None = None
    accounted: bool = False


@dataclass
class PairedLockState:
    initial: PaperPosition
    hedged: bool = False
    reversal_confirmations: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch rolling BTC 5m Up/Down Polymarket windows.")
    parser.add_argument("--slug", required=True, help="Current BTC 5m event slug or Polymarket event URL.")
    parser.add_argument("--duration", type=int, default=0, help="Total watch duration in seconds; 0 means unlimited.")
    parser.add_argument("--interval", type=int, default=10, help="Polling interval in seconds.")
    parser.add_argument("--price-source", default="POLYMARKET_CHAINLINK", help="POLYMARKET_CHAINLINK (strict default), AUTO (Chainlink + free exchanges), CHAINLINK, BINANCE, COINBASE, KRAKEN, or COINGECKO.")
    parser.add_argument("--edge", default="0.06", help="Minimum theoretical edge for BUY_UP/BUY_DOWN.")
    parser.add_argument("--fallback-sigma", default="0.00005", help="Fallback volatility per sqrt(second).")
    parser.add_argument("--clob-host", default="https://clob.polymarket.com")
    parser.add_argument("--market-data-timeout", type=int, default=3, help="Per-request timeout for CLOB and spot data.")
    parser.add_argument("--ws-proxy", help="Optional WebSocket proxy, e.g. socks5h://127.0.0.1:7898.")
    parser.add_argument("--record-jsonl", help="Append every complete market snapshot to this JSONL file.")
    parser.add_argument("--auto-trade", action="store_true", help="Enable automatic signal detection.")
    parser.add_argument("--live-trading", action="store_true", help="Actually submit orders. Requires wallet env vars.")
    parser.add_argument(
        "--strategy",
        default="fair_value_edge",
        choices=["near_even_momentum", "fair_value_edge", "paired_lock", "three_phase"],
    )
    parser.add_argument("--decision-seconds-before-end", type=int, default=90)
    parser.add_argument("--min-seconds-before-end", type=int, default=25)
    parser.add_argument("--signal-confirmations", type=int, default=2)
    parser.add_argument("--max-spot-age", type=int, default=20, help="Maximum cached spot-price age allowed for entries.")
    parser.add_argument("--max-start-capture-delay", type=int, default=15, help="Skip a window if its start price is captured later than this many seconds.")
    parser.add_argument("--min-win-probability", default="0.62")
    parser.add_argument("--min-entry", default="0.50")
    parser.add_argument("--max-entry", default="0.78")
    parser.add_argument("--max-spread", default="0.04", help="Max bid/ask spread allowed for the selected side.")
    parser.add_argument("--min-ask-sum", default="0.90", help="Skip markets where Up ask + Down ask is below this.")
    parser.add_argument("--max-ask-sum", default="1.10", help="Skip markets where Up ask + Down ask is above this.")
    parser.add_argument("--order-size", default="5")
    parser.add_argument("--max-trades", type=int, default=2, help="Max live/dry-run trade signals per window.")
    parser.add_argument(
        "--max-live-orders",
        type=int,
        default=0,
        help="Hard session cap on live order attempts; 0 means unlimited.",
    )
    parser.add_argument("--max-live-notional", default="3.50", help="Hard principal cap per live order in USDC.")
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
    parser.add_argument("--paired-entry-delay", type=int, default=10)
    parser.add_argument("--paired-initial-stake", default="0.50")
    parser.add_argument("--paired-profit-sum", default="0.90")
    parser.add_argument("--paired-emergency-sum", default="1.05")
    parser.add_argument("--paired-stop-loss", default="0.12")
    parser.add_argument("--paired-reversal-bps", default="2")
    parser.add_argument("--paired-stop-add-seconds", type=int, default=120)
    parser.add_argument("--paired-force-exit-seconds", type=int, default=60)
    parser.add_argument("--paired-no-hedge-seconds", type=int, default=30)
    parser.add_argument(
        "--three-phase-entry-start-seconds",
        type=int,
        default=95,
        help="Start accepting three_phase entries at this many seconds remaining.",
    )
    parser.add_argument(
        "--three-phase-entry-cutoff-seconds",
        type=int,
        default=40,
        help="Stop opening three_phase positions at this many seconds remaining.",
    )
    parser.add_argument("--three-phase-trend-threshold", default="0.25")
    parser.add_argument("--three-phase-reversal-ratio", default="1.20")
    parser.add_argument(
        "--three-phase-allow-reversals",
        action="store_true",
        help="Allow UD/DU reversal entries; disabled by default after paper-test underperformance.",
    )
    parser.add_argument("--three-phase-min-entry", default="0.35")
    parser.add_argument("--three-phase-max-entry", default="0.82")
    parser.add_argument("--three-phase-edge", default="0.03")
    parser.add_argument("--three-phase-confirmations", type=int, default=1)
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


def phase_trend(prices: list[Decimal], threshold: Decimal = Decimal("0.35")) -> tuple[str, Decimal]:
    if len(prices) < 2:
        return "N", Decimal("0")
    price_range = max(prices) - min(prices)
    if price_range <= 0:
        return "N", Decimal("0")
    score = (prices[-1] - prices[0]) / price_range
    if score >= threshold:
        return "U", score
    if score <= -threshold:
        return "D", score
    return "N", score


def choose_three_phase_signal(
    market: Market,
    phase_prices: list[list[Decimal]],
    start_price: Decimal,
    spot_price: Decimal,
    fair_probability_up: Decimal,
    up_quote: OrderBookQuote | None,
    down_quote: OrderBookQuote | None,
    edge: Decimal,
    max_spread: Decimal,
    trend_threshold: Decimal = Decimal("0.25"),
    reversal_ratio: Decimal = Decimal("1.20"),
    min_entry: Decimal = Decimal("0.35"),
    max_entry: Decimal = Decimal("0.82"),
    allow_reversals: bool = False,
) -> AutoTradeSignal | None:
    if len(phase_prices) < 2 or not phase_prices[0] or not phase_prices[1]:
        return None
    first, first_score = phase_trend(phase_prices[0], trend_threshold)
    second, second_score = phase_trend(phase_prices[1], trend_threshold)
    pattern = first + second
    side: str | None = None
    if pattern == "UU" and spot_price > start_price:
        side = "UP"
    elif pattern == "DD" and spot_price < start_price:
        side = "DOWN"
    elif (
        allow_reversals
        and pattern == "UD"
        and abs(second_score) >= abs(first_score) * reversal_ratio
        and spot_price < start_price
    ):
        side = "DOWN"
    elif (
        allow_reversals
        and pattern == "DU"
        and abs(second_score) >= abs(first_score) * reversal_ratio
        and spot_price > start_price
    ):
        side = "UP"
    if side is None:
        return None

    quote = up_quote if side == "UP" else down_quote
    if quote is None or quote.ask is None or quote.bid is None:
        return None
    if quote.ask < min_entry or quote.ask > max_entry or quote.ask - quote.bid > max_spread:
        return None
    probability = fair_probability_up if side == "UP" else Decimal("1") - fair_probability_up
    if probability - quote.ask < edge:
        return None
    return AutoTradeSignal(
        side=side,
        token_id=market.token_ids[0 if side == "UP" else 1],
        price=quote.ask,
        reason=(
            f"three_phase pattern={pattern} scores={first_score:.3f},{second_score:.3f} "
            f"probability={probability:.4f} edge={probability - quote.ask:.4f}"
        ),
    )


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
) -> AutoTradeSignal | None:
    if seconds_to_end > decision_seconds_before_end or seconds_to_end < min_seconds_before_end:
        return None
    ok, reason = quotes_pass_sanity_checks(up_quote, down_quote, max_spread, min_ask_sum, max_ask_sum)
    if not ok:
        return None
    assert up_quote is not None and up_quote.ask is not None
    assert down_quote is not None and down_quote.ask is not None

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
    if selected_probability < min_win_probability:
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
            f"p_up={probability_up.quantize(Decimal('0.0001'))} seconds_left={int(seconds_to_end)}"
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


def close_paper_position(position: PaperPosition, bankroll: Decimal, exit_price: Decimal, reason: str) -> Decimal:
    if position.settled:
        return bankroll
    proceeds = position.shares * exit_price
    position.settled = True
    position.profit = proceeds - position.stake
    bankroll += proceeds
    logger.info(
        "PAPER_CLOSE slug=%s side=%s exit=%s proceeds=%s profit=%s bankroll=%s reason=%s",
        position.slug,
        position.side,
        exit_price,
        proceeds.quantize(Decimal("0.0001")),
        position.profit.quantize(Decimal("0.0001")),
        bankroll.quantize(Decimal("0.0001")),
        reason,
    )
    return bankroll


def paired_lock_roi(entry_price: Decimal, hedge_price: Decimal) -> Decimal:
    total = entry_price + hedge_price
    if total <= 0:
        raise ValueError("Paired-lock prices must be positive")
    return (Decimal("1") - total) / total


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
    snapshot_writer = JsonlSnapshotWriter(Path(args.record_jsonl)) if args.record_jsonl else None
    slug = slug_from_value(args.slug)
    stop_at = float("inf") if args.duration == 0 else time.time() + args.duration
    current_market: Market | None = None
    start_price: Decimal | None = None
    initial_up_ask: Decimal | None = None
    last_spot_price: Decimal | None = None
    last_spot_fetched_at: float | None = None
    prices: list[Decimal] = []
    phase_prices: list[list[Decimal]] = [[], [], []]
    signals_this_window = 0
    candidate_side: str | None = None
    candidate_confirmations = 0
    edge_threshold = Decimal(args.edge)
    fallback_sigma = Decimal(args.fallback_sigma)
    min_entry = Decimal(args.min_entry)
    max_entry = Decimal(args.max_entry)
    max_spread = Decimal(args.max_spread)
    min_ask_sum = Decimal(args.min_ask_sum)
    max_ask_sum = Decimal(args.max_ask_sum)
    min_win_probability = Decimal(args.min_win_probability)
    order_size = Decimal(args.order_size)
    max_live_notional = Decimal(args.max_live_notional)
    decision_seconds_before_end = Decimal(str(args.decision_seconds_before_end))
    min_seconds_before_end = Decimal(str(args.min_seconds_before_end))
    paper_bankroll = Decimal(args.paper_bankroll)
    paper_stake = Decimal(args.paper_stake)
    paired_initial_stake = Decimal(args.paired_initial_stake)
    paired_profit_sum = Decimal(args.paired_profit_sum)
    paired_emergency_sum = Decimal(args.paired_emergency_sum)
    paired_stop_loss = Decimal(args.paired_stop_loss)
    paired_reversal_fraction = Decimal(args.paired_reversal_bps) / Decimal("10000")
    paper_positions: list[PaperPosition] = []
    paired_state: PairedLockState | None = None
    consecutive_losses = 0
    pause_windows_remaining = 0
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
    )
    _ACTIVE_NOTIFICATIONS = notifications
    atexit.register(notifications.stop, "进程退出")

    try:
        if args.live_trading and not args.auto_trade:
            raise ValueError("--live-trading requires --auto-trade")
        if args.duration < 0:
            raise ValueError("--duration must be zero (unlimited) or positive")
        if args.live_trading and args.strategy in {"paired_lock", "three_phase"}:
            raise ValueError(f"{args.strategy} is paper-only until its execution is validated")
        if args.live_trading and (
            args.max_live_orders < 0
            or args.max_trades < 1
            or order_size <= 0
            or max_live_notional <= 0
        ):
            raise ValueError(
                "Live order size, per-window limit, and notional must be positive; session limit may be zero"
            )
        if args.three_phase_entry_cutoff_seconds >= args.three_phase_entry_start_seconds:
            raise ValueError("three_phase entry cutoff must be lower than entry start")
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
        notifications.maybe_send_settlements(fetch_winner)
        notifications.maybe_send_daily(fetch_winner)
        if args.paper_trading:
            paper_bankroll = settle_all_paper_positions(paper_positions, paper_bankroll)
            consecutive_losses, new_pause_windows = account_new_paper_settlements(
                paper_positions,
                consecutive_losses,
                args.max_consecutive_losses,
                args.pause_windows_after_losses,
            )
            pause_windows_remaining = max(pause_windows_remaining, new_pause_windows)
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
            phase_prices = [[], [], []]
            signals_this_window = 0
            candidate_side = None
            candidate_confirmations = 0
            paired_state = None
            if pause_windows_remaining > 0:
                pause_windows_remaining -= 1
                logger.info("RISK_PAUSE_ACTIVE remaining_windows=%s", pause_windows_remaining)
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
            start_price = spot.price
            prices = [spot.price]
            logger.info("Captured start_price=%s for %s", start_price, current_market.slug)
        else:
            prices.append(spot.price)

        elapsed_seconds = max(Decimal("0"), Decimal("300") - seconds_to_end)
        phase_index = min(2, int(elapsed_seconds // Decimal("100")))
        phase_prices[phase_index].append(spot.price)

        sigma = estimate_sigma_per_sqrt_second(prices, Decimal(args.interval), fallback_sigma)
        fair = btc_up_probability(start_price, spot.price, max(Decimal("0"), seconds_to_end), sigma)
        up_quote, down_quote = quote_outcomes(clob, current_market)
        up_ask = up_quote.ask if up_quote else None
        down_ask = down_quote.ask if down_quote else None
        if initial_up_ask is None and up_ask is not None:
            initial_up_ask = up_ask
            logger.info("Captured initial_up_ask=%s for %s", initial_up_ask, current_market.slug)
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

        if args.auto_trade and args.strategy == "paired_lock" and pause_windows_remaining <= 0:
            elapsed_seconds = Decimal("300") - seconds_to_end
            quotes = {"UP": up_quote, "DOWN": down_quote}
            if paired_state is None and elapsed_seconds >= Decimal(str(args.paired_entry_delay)):
                side = "UP" if spot.price >= start_price else "DOWN"
                quote = quotes[side]
                if quote is not None and quote.ask is not None and quote.bid is not None:
                    spread = quote.ask - quote.bid
                    if spread <= max_spread and Decimal("0.20") <= quote.ask <= Decimal("0.80"):
                        if candidate_side == side:
                            candidate_confirmations += 1
                        else:
                            candidate_side = side
                            candidate_confirmations = 1
                        if candidate_confirmations >= max(1, args.signal_confirmations):
                            signal = AutoTradeSignal(
                                side=side,
                                token_id=current_market.token_ids[0 if side == "UP" else 1],
                                price=quote.ask,
                                reason=f"paired_lock opening momentum spot={spot.price} start={start_price}",
                            )
                            before = len(paper_positions)
                            paper_bankroll = open_paper_position(
                                paper_positions,
                                paper_bankroll,
                                current_market.slug,
                                signal,
                                paired_initial_stake,
                            )
                            if len(paper_positions) > before:
                                paired_state = PairedLockState(initial=paper_positions[-1])
                                signals_this_window += 1
                                logger.info("PAIRED_ENTRY %s", signal.reason)
                            candidate_side = None
                            candidate_confirmations = 0
                else:
                    candidate_side = None
                    candidate_confirmations = 0
            elif paired_state is not None and not paired_state.initial.settled and not paired_state.hedged:
                initial = paired_state.initial
                opposite = "DOWN" if initial.side == "UP" else "UP"
                initial_quote = quotes[initial.side]
                opposite_quote = quotes[opposite]
                if initial_quote is not None and opposite_quote is not None:
                    opposite_ask = opposite_quote.ask
                    initial_bid = initial_quote.bid
                    if opposite_ask is not None and seconds_to_end > Decimal(str(args.paired_no_hedge_seconds)):
                        combined = initial.entry_price + opposite_ask
                        emergency = (
                            seconds_to_end <= Decimal(str(args.paired_force_exit_seconds))
                            and combined <= paired_emergency_sum
                        )
                        if combined <= paired_profit_sum or emergency:
                            hedge_cost = initial.shares * opposite_ask
                            if paper_bankroll >= hedge_cost:
                                hedge_signal = AutoTradeSignal(
                                    side=opposite,
                                    token_id=current_market.token_ids[0 if opposite == "UP" else 1],
                                    price=opposite_ask,
                                    reason=f"paired_lock combined={combined} roi={paired_lock_roi(initial.entry_price, opposite_ask):.4f}",
                                )
                                paper_bankroll = open_paper_position(
                                    paper_positions,
                                    paper_bankroll,
                                    current_market.slug,
                                    hedge_signal,
                                    hedge_cost,
                                )
                                paired_state.hedged = True
                                logger.info("PAIRED_HEDGE %s", hedge_signal.reason)

                    if not paired_state.hedged:
                        reversed_spot = (
                            initial.side == "UP"
                            and spot.price <= start_price * (Decimal("1") - paired_reversal_fraction)
                        ) or (
                            initial.side == "DOWN"
                            and spot.price >= start_price * (Decimal("1") + paired_reversal_fraction)
                        )
                        stopped_price = (
                            initial_bid is not None
                            and initial_bid <= initial.entry_price * (Decimal("1") - paired_stop_loss)
                        )
                        paired_state.reversal_confirmations = (
                            paired_state.reversal_confirmations + 1 if reversed_spot and stopped_price else 0
                        )
                        force_exit = (
                            seconds_to_end <= Decimal(str(args.paired_force_exit_seconds)) and reversed_spot
                        )
                        if initial_bid is not None and (
                            paired_state.reversal_confirmations >= max(1, args.signal_confirmations) or force_exit
                        ):
                            paper_bankroll = close_paper_position(
                                initial,
                                paper_bankroll,
                                initial_bid,
                                "paired_lock reversal stop" if not force_exit else "paired_lock time exit",
                            )

        if (
            args.auto_trade
            and args.strategy != "paired_lock"
            and signals_this_window < args.max_trades
            and pause_windows_remaining <= 0
        ):
            if args.strategy == "near_even_momentum":
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
            elif args.strategy == "three_phase":
                signal = (
                    choose_three_phase_signal(
                        current_market,
                        phase_prices,
                        start_price,
                        spot.price,
                        fair.probability_up,
                        up_quote,
                        down_quote,
                        Decimal(args.three_phase_edge),
                        max_spread,
                        Decimal(args.three_phase_trend_threshold),
                        Decimal(args.three_phase_reversal_ratio),
                        Decimal(args.three_phase_min_entry),
                        Decimal(args.three_phase_max_entry),
                        args.three_phase_allow_reversals,
                    )
                    if (
                        Decimal(str(args.three_phase_entry_cutoff_seconds)) < seconds_to_end
                        <= Decimal(str(args.three_phase_entry_start_seconds))
                    )
                    else None
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
                )
            if signal is not None:
                if spot_age > Decimal(str(args.max_spot_age)):
                    logger.info("SIGNAL_REJECTED stale_spot_age=%.1fs", float(spot_age))
                    candidate_side = None
                    candidate_confirmations = 0
                    time.sleep(args.interval)
                    continue
                required_confirmations = (
                    args.three_phase_confirmations
                    if args.strategy == "three_phase"
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
                signals_this_window += 1
                candidate_side = None
                candidate_confirmations = 0
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
                    if not live_response_is_matched(response):
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
