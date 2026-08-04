from decimal import Decimal
from datetime import datetime, timedelta, timezone
import json
import math
import time

import pytest
from requests import HTTPError

from src.config import _slug
from src.backtest_updown import price_at_or_before, previous_slugs, winning_side, PricePoint
from src.fair_value import btc_up_probability, choose_theoretical_action, estimate_sigma_per_sqrt_second
from src.market_recorder import JsonlSnapshotWriter, build_snapshot
from src.polymarket import (
    GammaClient,
    ClobDataClient,
    ClobTradingClient,
    Market,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderBookQuote,
    OrderQuoteExpiredError,
    _parse_token_ids,
    choose_btc_markets,
    filter_markets_by_liquidity,
    rank_markets_by_liquidity,
)
from src.price_signal import (
    PolymarketChainlinkStream,
    SpotPrice,
    SpotPriceClient,
    build_threshold_signal,
    decode_chainlink_v3_price,
    extract_price_threshold,
)
from src.strategy import build_buy_intent
from src.watch_updown import (
    SignalConfirmationState,
    account_new_paper_settlements,
    accept_open_price,
    advance_signal_confirmation,
    adverse_jump_exceeds_dynamic_threshold,
    buy_limit_price_with_slippage,
    buy_limit_price_preserving_edge,
    consume_pause_window,
    effective_pullback_tolerance,
    evaluate_protective_hedge_risk,
    executable_ask_depth,
    fetch_reversal_completed_window_prices,
    fetch_reversal_chainlink_open_prices,
    choose_fair_value_edge_signal,
    choose_open_060_signal,
    choose_smart_score_signal,
    choose_market_reversal_hedge_signal,
    choose_protective_hedge_signal,
    choose_late_favorite_signal,
    late_spot_buffer_metrics,
    live_order_limit_reached,
    live_response_is_matched,
    live_session_should_continue,
    is_http_rate_limit,
    window_trade_count_after_attempt,
    window_priority_initialization_complete,
    open_paper_position,
    price_alignment_status,
    polling_interval_for_seconds_left,
    primary_signal_confirmation_count,
    refresh_open_060_signal,
    protective_open_cross_buffer,
    protective_spot_confirms_open_cross,
    quotes_pass_sanity_checks,
    response_fill_amounts,
    recent_spot_samples_support_side,
    rolling_realized_volatility,
    reversal_notifications_may_run,
    required_fair_value_edge,
    settle_all_paper_positions,
    settle_paper_positions,
    shrink_probability_toward_even,
    strategy_trade_limit,
    AutoTradeSignal,
    next_5m_slug,
    slug_from_value,
    parse_args as parse_watch_args,
)


def test_http_rate_limit_detection_requires_429_response() -> None:
    response = type("Response", (), {"status_code": 429})()
    error = HTTPError("too many requests", response=response)

    assert is_http_rate_limit(error)
    assert not is_http_rate_limit(RuntimeError("network failure"))


def make_market(
    question: str,
    slug: str,
    condition_id: str,
    token_ids: tuple[str, str],
    tick_size: str,
    neg_risk: bool,
    liquidity: Decimal,
) -> Market:
    return Market(
        question,
        slug,
        condition_id,
        token_ids,
        tick_size,
        neg_risk,
        liquidity,
        ("Up", "Down"),
        None,
        None,
    )


def test_fetch_reversal_chainlink_boundaries_seeds_two_previous_windows() -> None:
    class FakePriceClient:
        def __init__(self) -> None:
            self.calls = []

        def fetch(self, start_time, end_time):
            self.calls.append((start_time, end_time))
            return type(
                "PriceToBeat",
                (),
                {"open_price": Decimal(str(int(start_time.timestamp())))},
            )()

    start = datetime.fromtimestamp(700, timezone.utc)
    market = Market(
        "BTC Up or Down",
        "btc-updown-5m-700",
        "0xcondition",
        ("101", "202"),
        "0.01",
        False,
        Decimal("100"),
        ("Up", "Down"),
        start,
        start + timedelta(seconds=300),
    )
    client = FakePriceClient()

    prices = fetch_reversal_chainlink_open_prices(
        client,
        market,
        Decimal("700"),
        {"btc-updown-5m-100": Decimal("100")},
    )

    assert prices == {
        "btc-updown-5m-100": Decimal("100"),
        "btc-updown-5m-400": Decimal("400"),
        "btc-updown-5m-700": Decimal("700"),
    }
    assert client.calls == [
        (start - timedelta(seconds=300), start),
    ]


def test_fetch_reversal_chainlink_boundaries_supports_four_window_trigger() -> None:
    class FakePriceClient:
        def fetch(self, start_time, end_time):
            return type(
                "PriceToBeat",
                (),
                {"open_price": Decimal(str(int(start_time.timestamp())))},
            )()

    start = datetime.fromtimestamp(1300, timezone.utc)
    market = Market(
        "BTC Up or Down",
        "btc-updown-5m-1300",
        "0xcondition",
        ("101", "202"),
        "0.01",
        False,
        Decimal("100"),
        ("Up", "Down"),
        start,
        start + timedelta(seconds=300),
    )

    prices = fetch_reversal_chainlink_open_prices(
        FakePriceClient(), market, Decimal("1300"), lookback_windows=4
    )

    assert list(prices) == [
        "btc-updown-5m-100",
        "btc-updown-5m-400",
        "btc-updown-5m-700",
        "btc-updown-5m-1000",
        "btc-updown-5m-1300",
    ]


def test_fetch_reversal_completed_prices_only_returns_final_missing_windows() -> None:
    class FakePriceClient:
        def __init__(self) -> None:
            self.calls = []

        def fetch(self, start_time, end_time):
            self.calls.append((start_time, end_time))
            return type(
                "PriceToBeat",
                (),
                {
                    "open_price": Decimal("101"),
                    "close_price": Decimal("99"),
                    "completed": True,
                    "incomplete": False,
                },
            )()

    start = datetime.fromtimestamp(700, timezone.utc)
    market = Market(
        "BTC Up or Down",
        "btc-updown-5m-700",
        "0xcondition",
        ("101", "202"),
        "0.01",
        False,
        Decimal("100"),
        ("Up", "Down"),
        start,
        start + timedelta(seconds=300),
    )
    client = FakePriceClient()

    prices = fetch_reversal_completed_window_prices(
        client,
        market,
        "btc-updown-5m-100",
    )

    assert prices == {"btc-updown-5m-400": (Decimal("101"), Decimal("99"))}
    assert client.calls == [
        (datetime.fromtimestamp(400, timezone.utc), datetime.fromtimestamp(700, timezone.utc))
    ]


def test_fetch_reversal_completed_prices_supports_four_window_trigger() -> None:
    class FakePriceClient:
        def fetch(self, start_time, end_time):
            return type(
                "PriceToBeat",
                (),
                {
                    "open_price": Decimal("101"),
                    "close_price": Decimal("102"),
                    "completed": True,
                    "incomplete": False,
                },
            )()

    start = datetime.fromtimestamp(1300, timezone.utc)
    market = Market(
        "BTC Up or Down",
        "btc-updown-5m-1300",
        "0xcondition",
        ("101", "202"),
        "0.01",
        False,
        Decimal("100"),
        ("Up", "Down"),
        start,
        start + timedelta(seconds=300),
    )

    prices = fetch_reversal_completed_window_prices(
        FakePriceClient(), market, None, lookback_windows=4
    )

    assert list(prices) == [
        "btc-updown-5m-100",
        "btc-updown-5m-400",
        "btc-updown-5m-700",
        "btc-updown-5m-1000",
    ]


def test_reversal_accepts_first_valid_price_to_beat_without_stability_wait() -> None:
    from src.price_alignment import StableOpenPriceTracker

    tracker = StableOpenPriceTracker(
        required_confirmations=2,
        minimum_stable_seconds=5.0,
    )

    accepted = accept_open_price(
        "reversal_v11",
        tracker,
        Decimal("63749.33434881677"),
        100.0,
    )

    assert accepted == Decimal("63749.33434881677")
    assert tracker.confirmations == 0

def test_other_strategies_keep_stable_price_to_beat_confirmation() -> None:
    from src.price_alignment import StableOpenPriceTracker

    tracker = StableOpenPriceTracker(
        required_confirmations=2,
        minimum_stable_seconds=5.0,
    )

    assert (
        accept_open_price(
            "fair_value_edge",
            tracker,
            Decimal("63749.33434881677"),
            100.0,
        )
        is None
    )


def test_slow_maintenance_waits_until_window_and_reversal_boundary_are_ready() -> None:
    now = datetime(2026, 7, 29, 0, 0, 10, tzinfo=timezone.utc)
    market = Market(
        "BTC Up or Down",
        "btc-updown-5m-1000",
        "0xcondition",
        ("101", "202"),
        "0.01",
        False,
        Decimal("100"),
        ("Up", "Down"),
        now - timedelta(seconds=10),
        now + timedelta(seconds=290),
    )

    assert not window_priority_initialization_complete(
        market, now, None, "reversal_v11", None
    )
    assert not window_priority_initialization_complete(
        market, now, Decimal("100"), "reversal_v11", None
    )
    assert window_priority_initialization_complete(
        market,
        now,
        Decimal("100"),
        "reversal_v11",
        market.slug,
    )
    assert window_priority_initialization_complete(
        market, now, Decimal("100"), "fair_value_edge", None
    )


@pytest.mark.parametrize(
    "status",
    [
        "entry_unmatched",
        "entry_amount_rejected",
        "entry_partial",
        "entry_book_pending",
        "entry_balance_insufficient",
        "exit_unmatched",
        "exit_partial",
        "exit_book_pending",
        "exit_balance_pending",
        "waiting_chainlink_boundary_no_split",
    ],
)
def test_reversal_notifications_wait_while_order_work_is_pending(status: str) -> None:
    assert not reversal_notifications_may_run(status)


@pytest.mark.parametrize(
    "status",
    [
        "entry_complete",
        "entry_reconciled",
        "exit_complete",
        "awaiting_settlement",
        "no_trigger_no_split",
        "opening_already_processed",
    ],
)
def test_reversal_notifications_run_only_after_order_path_is_complete(status: str) -> None:
    assert reversal_notifications_may_run(status)


def test_rolling_realized_volatility_uses_cross_window_timestamped_samples() -> None:
    samples = [
        (940.0, Decimal("100")),
        (970.0, Decimal("101")),
        (1000.0, Decimal("100")),
    ]

    result = rolling_realized_volatility(samples, observed_at=1000.0)

    assert result is not None
    expected = Decimal(
        str(
            math.sqrt(
                math.log(1.01) ** 2
                + math.log(Decimal("100") / Decimal("101")) ** 2
            )
        )
    )
    assert abs(result - expected) < Decimal("0.000000000001")


def test_parse_token_ids_accepts_json_string() -> None:
    assert _parse_token_ids('["yes-token", "no-token"]') == ("yes-token", "no-token")


def test_choose_btc_markets_matches_question_and_slug() -> None:
    markets = [
        make_market("Bitcoin up today?", "btc-up", "c1", ("y", "n"), "0.01", False, Decimal("10")),
        make_market("Fed decision?", "fed", "c2", ("y", "n"), "0.01", False, Decimal("10")),
    ]
    assert choose_btc_markets(markets, ("bitcoin", "btc"), ("up", "down")) == [markets[0]]


def test_slug_accepts_polymarket_urls() -> None:
    assert _slug("https://polymarket.com/event/bitcoin-up-or-down", "event") == "bitcoin-up-or-down"
    assert _slug("https://polymarket.com/market/bitcoin-above-100k", "market") == "bitcoin-above-100k"
    assert _slug("bitcoin-daily", "event") == "bitcoin-daily"


def test_market_liquidity_filter_and_rank() -> None:
    markets = [
        make_market("Bitcoin up?", "low", "c1", ("y", "n"), "0.01", False, Decimal("5")),
        make_market("Bitcoin down?", "high", "c2", ("y", "n"), "0.01", False, Decimal("50")),
    ]
    filtered = filter_markets_by_liquidity(markets, Decimal("10"))
    assert filtered == [markets[1]]
    assert rank_markets_by_liquidity(markets) == [markets[1], markets[0]]


def test_event_by_slug_parses_nested_markets(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return [
                {
                    "title": "Bitcoin daily",
                    "slug": "bitcoin-daily",
                    "markets": [
                        {
                            "question": "Will Bitcoin be above $100,000?",
                            "slug": "bitcoin-above-100k",
                            "conditionId": "condition",
                            "clobTokenIds": '["yes", "no"]',
                            "minimum_tick_size": "0.01",
                            "negRisk": False,
                        }
                    ],
                }
            ]

    def fake_get(*args, **kwargs):
        return Response()

    monkeypatch.setattr("src.polymarket.requests.get", fake_get)

    event = GammaClient().event_by_slug("bitcoin-daily")

    assert event.slug == "bitcoin-daily"
    assert len(event.markets) == 1
    assert event.markets[0].token_ids == ("yes", "no")
    assert event.markets[0].outcomes == ("Yes", "No")


def test_strategy_builds_intent_under_max_price() -> None:
    market = make_market("Bitcoin up today?", "btc-up", "c1", ("yes", "no"), "0.01", False, Decimal("10"))
    intent = build_buy_intent(
        market,
        "YES",
        OrderBookQuote(bid=Decimal("0.49"), ask=Decimal("0.50")),
        Decimal("0.55"),
        Decimal("5"),
    )
    assert intent is not None
    assert intent.token_id == "yes"
    assert intent.price == Decimal("0.50")


def test_extract_price_threshold_handles_commas_and_suffixes() -> None:
    assert extract_price_threshold("Will bitcoin be above $110,000?") == Decimal("110000")
    assert extract_price_threshold("Will bitcoin hit $1m?") == Decimal("1000000")


def test_threshold_signal_respects_buffer() -> None:
    signal = build_threshold_signal(
        "Will bitcoin reach $100,000?",
        SpotPrice("BTC/USD", Decimal("101000"), "TEST"),
        Decimal("25"),
    )
    assert signal is not None
    assert signal.outcome == "YES"


def test_downside_threshold_signal_reverses_direction() -> None:
    signal = build_threshold_signal(
        "Will bitcoin dip to $45,000?",
        SpotPrice("BTC/USD", Decimal("64399"), "TEST"),
        Decimal("25"),
    )
    assert signal is not None
    assert signal.outcome == "NO"


def test_coingecko_spot_price_parser(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"bitcoin": {"usd": 64399}}

    def fake_get(*args, **kwargs):
        return Response()

    monkeypatch.setattr("src.price_signal.requests.get", fake_get)

    price = SpotPriceClient("COINGECKO").btc_usd()

    assert price.price == Decimal("64399")
    assert price.source == "COINGECKO"


def test_spot_price_falls_back_after_source_error(monkeypatch) -> None:
    class Response:
        def __init__(self, payload, status_code=200) -> None:
            self.payload = payload
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise HTTPError("rate limited")

        def json(self):
            return self.payload

    def fake_get(url, params=None, timeout=None):
        if "coingecko" in url:
            return Response({}, 429)
        if "binance" in url:
            return Response({"price": "64123.45"})
        raise AssertionError(url)

    monkeypatch.setattr("src.price_signal.requests.get", fake_get)

    price = SpotPriceClient("COINGECKO").btc_usd()

    assert price.price == Decimal("64123.45")
    assert price.source == "BINANCE"


def test_kraken_spot_price_parser(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"error": [], "result": {"XXBTZUSD": {"c": ["64111.25", "1"]}}}

    monkeypatch.setattr("src.price_signal.requests.get", lambda *args, **kwargs: Response())

    price = SpotPriceClient("KRAKEN").btc_usd()

    assert price.price == Decimal("64111.25")
    assert price.source == "KRAKEN"


def test_polymarket_chainlink_stream_parser() -> None:
    message = (
        '{"topic":"crypto_prices_chainlink","type":"update","timestamp":1753314088421,'
        '"payload":{"symbol":"btc/usd","timestamp":1753314088395,"value":67234.50}}'
    )

    price = PolymarketChainlinkStream.parse_message(message)

    assert price is not None
    assert price.price == Decimal("67234.5")
    assert price.source == "POLYMARKET_CHAINLINK"
    assert price.observed_at == 1753314088
    assert price.observed_at_ms == 1753314088395


def test_polymarket_chainlink_stream_selects_nearest_boundary_sample() -> None:
    stream = PolymarketChainlinkStream()
    stream._history.extend(
        [
            SpotPrice(
                "BTC/USD",
                Decimal("63914.40"),
                "POLYMARKET_CHAINLINK",
                observed_at=1784356799,
                observed_at_ms=1784356799400,
            ),
            SpotPrice(
                "BTC/USD",
                Decimal("63914.48"),
                "POLYMARKET_CHAINLINK",
                observed_at=1784356800,
                observed_at_ms=1784356800120,
            ),
        ]
    )

    price = stream.price_near(1784356800000, max_distance_ms=500)

    assert price.price == Decimal("63914.48")
    assert price.observed_at_ms == 1784356800120


def test_polymarket_chainlink_stream_rejects_distant_boundary_sample() -> None:
    stream = PolymarketChainlinkStream()
    stream._history.append(
        SpotPrice(
            "BTC/USD",
            Decimal("63914.48"),
            "POLYMARKET_CHAINLINK",
            observed_at=1784356802,
            observed_at_ms=1784356802500,
        )
    )

    try:
        stream.price_near(1784356800000, max_distance_ms=1000)
    except RuntimeError as exc:
        assert "2500ms" in str(exc)
    else:
        raise AssertionError("A distant Chainlink sample must not verify the boundary")


def test_official_open_price_accepts_missing_boundary_audit_sample() -> None:
    status, difference = price_alignment_status(
        Decimal("64000"),
        None,
        Decimal("0.50"),
    )

    assert status == "UNVERIFIED_BOUNDARY_SAMPLE"
    assert difference is None


def test_official_open_price_records_boundary_mismatch_without_rejection() -> None:
    status, difference = price_alignment_status(
        Decimal("64000"),
        Decimal("64004.25"),
        Decimal("0.50"),
    )

    assert status == "MISMATCH_WARNING"
    assert difference == Decimal("4.25")


def test_polymarket_chainlink_stream_rejects_stale_cached_price() -> None:
    stream = PolymarketChainlinkStream(timeout=0, max_stale_seconds=15)
    stream._started = True
    stream._latest = SpotPrice(
        symbol="BTC/USD",
        price=Decimal("64123.45"),
        source="POLYMARKET_CHAINLINK",
        observed_at=int(time.time()) - 16,
    )
    stream._ready.set()

    try:
        stream.btc_usd()
    except RuntimeError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("Expected stale Chainlink data to be rejected")


def test_spot_client_rebuilds_chainlink_stream_after_three_failures() -> None:
    class FailingStream:
        def __init__(self) -> None:
            self.closed = False

        def btc_usd(self) -> SpotPrice:
            raise RuntimeError("stream unavailable")

        def close(self) -> None:
            self.closed = True

    stream = FailingStream()
    client = SpotPriceClient("POLYMARKET_CHAINLINK")
    client._polymarket_stream = stream

    for _ in range(3):
        with pytest.raises(RuntimeError, match="stream unavailable"):
            client.btc_usd()

    assert stream.closed
    assert client._polymarket_stream is None


def test_paper_position_fee_is_deducted_from_bankroll_and_profit(monkeypatch) -> None:
    positions = []
    signal = AutoTradeSignal(side="UP", token_id="up", price=Decimal("0.75"), reason="late")
    bankroll = open_paper_position(
        positions,
        Decimal("20"),
        "slug",
        signal,
        Decimal("1"),
        fee_rate=Decimal("0.07"),
    )

    assert positions[0].fee == Decimal("0.0175")
    assert bankroll == Decimal("18.9825")

    monkeypatch.setattr("src.watch_updown.fetch_winner", lambda slug: "UP")
    bankroll = settle_paper_positions(positions, "slug", bankroll)

    assert positions[0].profit.quantize(Decimal("0.0001")) == Decimal("0.3158")
    assert bankroll.quantize(Decimal("0.0001")) == Decimal("20.3158")


def test_jsonl_snapshot_writer_preserves_quotes(tmp_path) -> None:
    snapshot = build_snapshot(
        observed_at="2026-07-14T00:00:00+00:00",
        observed_ts=100,
        slug="btc-updown-5m-0",
        market_start_ts=0,
        market_end_ts=300,
        seconds_left=Decimal("90.9"),
        spot=Decimal("62000.25"),
        start_spot=Decimal("61990"),
        spot_source="POLYMARKET_CHAINLINK",
        probability_up=Decimal("0.65"),
        up_quote=OrderBookQuote(Decimal("0.59"), Decimal("0.60")),
        down_quote=OrderBookQuote(Decimal("0.39"), Decimal("0.40")),
    )
    path = tmp_path / "snapshots.jsonl"

    JsonlSnapshotWriter(path).write(snapshot)

    item = json.loads(path.read_text())
    assert item["seconds_left"] == 90
    assert item["up_ask"] == "0.60"
    assert item["spot_source"] == "POLYMARKET_CHAINLINK"


def test_live_fok_order_uses_two_step_sdk_path(monkeypatch) -> None:
    calls = []

    class OrderArgs:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class OrderType:
        GTC = "GTC"
        FOK = "FOK"

    class Options:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class Client:
        def create_and_post_order(self, *args, **kwargs):
            raise AssertionError("FOK must not use the GTC convenience path")

        def create_order(self, order_args, options=None):
            calls.append(("create", order_args.kwargs, options.kwargs))
            return "signed"

        def post_order(self, order, order_type):
            calls.append(("post", order, order_type))
            return {"success": True, "status": "matched"}

    monkeypatch.setattr(
        "src.polymarket._import_order_types",
        lambda: (OrderArgs, OrderType, Options, "BUY"),
    )
    trader = object.__new__(ClobTradingClient)
    trader.client = Client()

    response = trader.buy_limit(
        token_id="down",
        price=Decimal("0.48"),
        size=Decimal("5"),
        tick_size="0.01",
        neg_risk=False,
        order_type="FOK",
    )

    assert response["status"] == "matched"
    assert calls[-1] == ("post", "signed", "FOK")


def test_live_fok_order_uses_v2_order_type(monkeypatch) -> None:
    calls = []

    class OrderArgs:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class OrderType:
        GTC = "GTC"
        FOK = "FOK"

    class Options:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class Client:
        def create_and_post_order(self, order_args, options=None, order_type=None):
            calls.append((order_args.kwargs, options.kwargs, order_type))
            return {"success": True, "status": "matched"}

    monkeypatch.setattr(
        "src.polymarket._import_order_types",
        lambda: (OrderArgs, OrderType, Options, "BUY"),
    )
    trader = object.__new__(ClobTradingClient)
    trader.client = Client()
    trader._client_v2 = True

    response = trader.buy_limit(
        token_id="up",
        price=Decimal("0.68"),
        size=Decimal("5"),
        tick_size="0.01",
        neg_risk=False,
        order_type="FOK",
    )

    assert response["status"] == "matched"
    assert calls[-1][2] == "FOK"


def test_live_fak_order_uses_v2_order_type(monkeypatch) -> None:
    calls = []

    class OrderArgs:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class OrderType:
        GTC = "GTC"
        FAK = "FAK"

    class Options:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class Client:
        def create_and_post_order(self, order_args, options=None, order_type=None):
            calls.append((order_args.kwargs, options.kwargs, order_type))
            return {
                "success": True,
                "status": "matched",
                "orderID": "0x1",
                "makingAmount": "1.25",
                "takingAmount": "2.5",
            }

    monkeypatch.setattr(
        "src.polymarket._import_order_types",
        lambda: (OrderArgs, OrderType, Options, "BUY"),
    )
    trader = object.__new__(ClobTradingClient)
    trader.client = Client()
    trader._client_v2 = True

    response = trader.buy_limit(
        token_id="up",
        price=Decimal("0.50"),
        size=Decimal("5"),
        tick_size="0.01",
        neg_risk=False,
        order_type="FAK",
    )

    assert response["takingAmount"] == "2.5"
    assert calls[-1][2] == "FAK"


def test_live_post_only_order_uses_v2_flag(monkeypatch) -> None:
    calls = []

    class OrderArgs:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class OrderType:
        GTC = "GTC"

    class Options:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class Client:
        def create_and_post_order(self, order_args, options=None, order_type=None, post_only=False):
            calls.append((order_args.kwargs, order_type, post_only))
            return {"success": True, "status": "live", "orderID": "maker-1"}

    monkeypatch.setattr(
        "src.polymarket._import_order_types",
        lambda: (OrderArgs, OrderType, Options, "BUY"),
    )
    trader = object.__new__(ClobTradingClient)
    trader.client = Client()
    trader._client_v2 = True

    response = trader.buy_limit(
        token_id="up",
        price=Decimal("0.45"),
        size=Decimal("5"),
        tick_size="0.01",
        neg_risk=False,
        post_only=True,
    )

    assert response["orderID"] == "maker-1"
    assert calls == [({"token_id": "up", "price": 0.45, "size": 5.0, "side": "BUY"}, "GTC", True)]


def test_live_v2_order_blocks_post_when_final_quote_expires(monkeypatch) -> None:
    calls = []

    class OrderArgs:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class OrderType:
        FAK = "FAK"

    class Options:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class Client:
        def create_order(self, order_args, options=None):
            calls.append(("create", order_args.kwargs, options.kwargs))
            return "signed"

        def post_order(self, order, order_type):
            calls.append(("post", order, order_type))
            return {"success": True}

    monkeypatch.setattr(
        "src.polymarket._import_order_types",
        lambda: (OrderArgs, OrderType, Options, "BUY"),
    )
    trader = object.__new__(ClobTradingClient)
    trader.client = Client()
    trader._client_v2 = True

    with pytest.raises(OrderQuoteExpiredError):
        trader.buy_limit(
            token_id="up",
            price=Decimal("0.50"),
            size=Decimal("5"),
            tick_size="0.01",
            neg_risk=False,
            order_type="FAK",
            submit_not_after_monotonic=time.monotonic() - 1,
        )

    assert [call[0] for call in calls] == ["create"]


def test_live_fok_sell_uses_sell_side(monkeypatch) -> None:
    calls = []

    class OrderArgs:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class OrderType:
        GTC = "GTC"
        FOK = "FOK"

    class Options:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class Client:
        def create_and_post_order(self, order_args, options=None, order_type=None):
            calls.append((order_args.kwargs, options.kwargs, order_type))
            return {"success": True, "status": "matched"}

    monkeypatch.setattr(
        "src.polymarket._import_sell_order_types",
        lambda: (OrderArgs, OrderType, Options, "SELL"),
    )
    trader = object.__new__(ClobTradingClient)
    trader.client = Client()
    trader._client_v2 = True

    response = trader.sell_limit(
        token_id="up",
        price=Decimal("0.67"),
        size=Decimal("5"),
        tick_size="0.01",
        neg_risk=False,
        order_type="FOK",
    )

    assert response["status"] == "matched"
    assert calls[-1][0]["side"] == "SELL"
    assert calls[-1][2] == "FOK"


def test_trading_quote_selects_best_prices_from_v2_book_order() -> None:
    class Client:
        def get_order_book(self, token_id):
            assert token_id == "up"
            return {
                "bids": [{"price": "0.01"}, {"price": "0.46"}, {"price": "0.45"}],
                "asks": [{"price": "0.99"}, {"price": "0.48"}, {"price": "0.47"}],
            }

    trader = object.__new__(ClobTradingClient)
    trader.client = Client()

    assert trader.quote("up") == OrderBookQuote(bid=Decimal("0.46"), ask=Decimal("0.47"))


def test_live_session_defaults_to_unlimited_orders_and_two_per_window(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["watch_updown", "--slug", "btc-updown-5m-1"])

    args = parse_watch_args()

    assert args.strategy == "reversal_v11"
    assert args.max_live_orders == 0
    assert args.max_trades == 2
    assert args.duration == 0
    assert args.decision_seconds_before_end == 120
    assert args.min_seconds_before_end == 25
    assert args.min_entry == "0.50"
    assert args.trend_confirmation_samples == 3
    assert args.one_way_entry_seconds == 100
    assert args.one_way_entry_cutoff_seconds == 25
    assert args.trend_pullback_tolerance_usd == "1.00"
    assert args.trend_pullback_tolerance_percent == "25"
    assert args.hedge_signal_confirmations == 2
    assert args.hedge_confirmation_min_seconds == 2.0
    assert args.hedge_max_price_worsening == "0.05"
    assert args.hedge_entry_start_seconds == 300
    assert args.hedge_entry_cutoff_seconds == 1
    assert args.hedge_open_cross_min_usd == "1.00"
    assert args.hedge_open_cross_sigma_multiplier == "1.00"
    assert args.hedge_max_spread == "0.10"
    assert args.hedge_market_reversal_threshold == "0.55"
    assert args.hedge_min_win_probability == "0.53"
    assert args.hedge_min_edge == "0.01"
    assert args.hedge_fee_rate == "0.07"
    assert args.post_fill_poll_interval == 1.0
    assert args.pre_submit_max_adverse_ask_drop == "0.02"
    assert args.pre_submit_max_ask_worsening == "0.02"
    assert args.pre_submit_max_quote_age_seconds == 1.0
    assert args.max_entry == "0.78"
    assert args.max_live_notional == "4.05"
    assert args.late_max_live_notional == "4.70"
    assert args.low_entry_cutoff == "0.55"
    assert args.low_entry_min_win_probability == "0.61"
    assert args.low_entry_confirmation_samples == 3
    assert args.probability_shrinkage == "1.00"
    assert args.smart_score_threshold == "70"
    assert args.smart_score_entry_seconds == 100
    assert args.smart_score_cutoff_seconds == 25
    assert args.smart_score_min_probability == "0.52"
    assert args.smart_score_fee_rate == "0.07"
    assert args.smart_score_slippage == "0.01"
    assert args.smart_score_trend_samples == 3
    assert args.smart_score_stability_samples == 3
    assert args.open_060_entry_seconds == 300
    assert args.open_060_cutoff_seconds == 270
    assert args.open_060_target == "0.60"
    assert args.open_060_slippage == "0.01"
    assert args.open_060_fee_rate == "0.07"
    assert args.open_060_initial_ask == "0.50"
    assert args.paper_shares == "0"
    assert args.disable_telegram_commands is False
    assert args.disable_discord is False
    assert args.min_win_probability == "0.55"
    assert args.edge == "0.02"
    assert args.max_spread == "0.05"
    assert args.official_open_confirmations == 2
    assert args.official_open_stable_seconds == 5.0
    assert args.live_order_type == "FAK"
    assert args.live_buy_slippage == "0.03"
    assert args.late_entry_start_seconds == 55
    assert args.late_entry_cutoff_seconds == 8
    assert args.late_min_entry == "0.65"
    assert args.late_max_entry == "0.94"
    assert args.late_min_win_probability == "0.80"
    assert args.late_edge_margin == "0.00"
    assert args.late_min_expected_roi == "0.02"
    assert args.late_fee_rate == "0.07"
    assert args.late_max_spread == "0.03"
    assert args.late_min_ask_sum == "0.96"
    assert args.late_max_ask_sum == "1.04"
    assert args.late_confirmation_samples == 2
    assert args.late_no_cross_samples == 3
    assert args.late_signal_confirmations == 1
    assert args.late_min_lead_bps == "1.0"
    assert args.late_max_pullback_bps == "1.50"
    assert args.late_max_pullback_ratio == "0.50"
    assert args.late_volatility_buffer_multiplier == "0.50"
    assert args.late_pause_windows_after_loss == 0


def test_buy_limit_slippage_adds_three_ticks_and_respects_execution_cap() -> None:
    assert buy_limit_price_with_slippage(
        Decimal("0.61"), Decimal("0.03"), "0.01", Decimal("0.81")
    ) == Decimal("0.64")
    assert buy_limit_price_with_slippage(
        Decimal("0.79"), Decimal("0.03"), "0.01", Decimal("0.81")
    ) == Decimal("0.81")


def test_buy_limit_slippage_never_crosses_one() -> None:
    assert buy_limit_price_with_slippage(
        Decimal("0.98"), Decimal("0.03"), "0.01", Decimal("1.02")
    ) == Decimal("0.99")


def test_buy_limit_slippage_preserves_required_edge() -> None:
    assert required_fair_value_edge(
        Decimal("0.60"), Decimal("89"), Decimal("0.06")
    ) == Decimal("0.06")
    assert buy_limit_price_preserving_edge(
        Decimal("0.60"),
        Decimal("0.03"),
        "0.01",
        Decimal("0.81"),
        Decimal("0.6681"),
        Decimal("89"),
        Decimal("0.06"),
    ) == Decimal("0.60")
    assert buy_limit_price_preserving_edge(
        Decimal("0.60"),
        Decimal("0.03"),
        "0.01",
        Decimal("0.81"),
        Decimal("0.72"),
        Decimal("89"),
        Decimal("0.06"),
    ) == Decimal("0.63")


def test_live_response_requires_conclusive_match() -> None:
    assert live_response_is_matched({"success": True, "status": "matched", "orderID": "0x1"}) is True
    assert live_response_is_matched({"success": True, "status": "live", "orderID": "0x1"}) is False
    assert live_response_is_matched({"success": False, "status": "matched", "orderID": "0x1"}) is False
    assert live_response_is_matched(
        {
            "success": True,
            "status": "matched",
            "orderID": "0x1",
            "makingAmount": "1.25",
            "takingAmount": "2.5",
        },
        require_fill_amounts=True,
    ) is True
    assert live_response_is_matched(
        {"success": True, "status": "matched", "orderID": "0x1"},
        require_fill_amounts=True,
    ) is False


def test_protective_confirmation_requires_count_duration_direction_and_price() -> None:
    state = SignalConfirmationState()
    first = AutoTradeSignal("DOWN", "down", Decimal("0.40"), "protective_hedge edge=0.10")
    improved = AutoTradeSignal("DOWN", "down", Decimal("0.38"), "protective_hedge edge=0.12")

    ready, _ = advance_signal_confirmation(state, first, 100.0, 2, 5.0, Decimal("0.05"))
    assert ready is False
    ready, _ = advance_signal_confirmation(state, improved, 101.0, 2, 5.0, Decimal("0.05"))
    assert ready is False
    ready, _ = advance_signal_confirmation(state, improved, 105.0, 2, 5.0, Decimal("0.05"))
    assert ready is True

    opposite = AutoTradeSignal("UP", "up", Decimal("0.40"), "protective_hedge edge=0.10")
    ready, _ = advance_signal_confirmation(state, opposite, 106.0, 2, 5.0, Decimal("0.05"))
    assert ready is False
    assert state.side == "UP"
    assert state.confirmations == 1


def test_protective_confirmation_restarts_after_price_worsens() -> None:
    state = SignalConfirmationState()
    first = AutoTradeSignal("DOWN", "down", Decimal("0.40"), "protective_hedge edge=0.10")
    worse = AutoTradeSignal("DOWN", "down", Decimal("0.46"), "protective_hedge edge=0.08")

    advance_signal_confirmation(state, first, 100.0, 2, 5.0, Decimal("0.05"))
    ready, _ = advance_signal_confirmation(state, worse, 106.0, 2, 5.0, Decimal("0.05"))

    assert ready is False
    assert state.confirmations == 1
    assert state.started_at == 106.0
    assert state.initial_price == Decimal("0.46")


def test_protective_open_cross_buffer_uses_larger_dynamic_value() -> None:
    assert protective_open_cross_buffer(
        Decimal("100"),
        Decimal("0.01"),
        Decimal("4"),
        Decimal("2"),
        Decimal("1"),
    ) == Decimal("2")
    assert protective_open_cross_buffer(
        Decimal("100"),
        Decimal("0.02"),
        Decimal("4"),
        Decimal("2"),
        Decimal("1"),
    ) == Decimal("4.00")


def test_protective_spot_confirmation_starts_on_cross_and_rejects_narrowing() -> None:
    start = Decimal("100")
    buffer = Decimal("2")

    assert protective_spot_confirms_open_cross(
        [Decimal("99"), Decimal("98")], start, "DOWN", buffer
    ) is True
    assert protective_spot_confirms_open_cross(
        [Decimal("97"), Decimal("98")], start, "DOWN", buffer
    ) is False
    assert protective_spot_confirms_open_cross(
        [Decimal("101"), Decimal("102")], start, "UP", buffer
    ) is True
    assert protective_spot_confirms_open_cross(
        [Decimal("103"), Decimal("102")], start, "UP", buffer
    ) is False
    assert protective_spot_confirms_open_cross(
        [Decimal("101.99")], start, "UP", buffer
    ) is False


def test_only_matched_live_orders_consume_window_trade_slots() -> None:
    assert window_trade_count_after_attempt(0, live=True, matched=False) == 0
    assert window_trade_count_after_attempt(1, live=True, matched=False) == 1
    assert window_trade_count_after_attempt(0, live=True, matched=True) == 1
    assert window_trade_count_after_attempt(1, live=True, matched=True) == 2
    assert window_trade_count_after_attempt(0, live=False) == 1


def test_zero_live_order_limit_means_unlimited() -> None:
    assert live_order_limit_reached(100, 0) is False
    assert live_order_limit_reached(1, 2) is False
    assert live_order_limit_reached(2, 2) is True
    assert live_session_should_continue(100, 0) is True
    assert live_session_should_continue(2, 2) is False


def build_chainlink_v3_report(feed_id: str, price: int) -> str:
    word = lambda value: int(value).to_bytes(32, "big", signed=False)
    signed_word = lambda value: int(value).to_bytes(32, "big", signed=True)
    payload = b"".join(
        [
            bytes.fromhex(feed_id.removeprefix("0x")),
            word(100),
            word(101),
            word(0),
            word(0),
            word(200),
            signed_word(price),
            signed_word(price - 1),
            signed_word(price + 1),
        ]
    )
    outer = b"\x00" * (3 * 32) + word(7 * 32) + word(0) + word(0) + word(0)
    return "0x" + (outer + word(len(payload)) + payload).hex()


def test_decode_chainlink_v3_price() -> None:
    feed_id = "0x" + "12" * 32
    full_report = build_chainlink_v3_report(feed_id, 6412345 * 10**16)

    assert decode_chainlink_v3_price(full_report, feed_id) == Decimal("64123.45")


def test_chainlink_spot_price_authenticates_and_decodes(monkeypatch) -> None:
    feed_id = "0x" + "34" * 32
    full_report = build_chainlink_v3_report(feed_id, 64000 * 10**18)
    captured = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"report": {"fullReport": full_report, "observationsTimestamp": 1234567890}}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        return Response()

    monkeypatch.setenv("CHAINLINK_DATA_STREAMS_API_KEY", "api-key")
    monkeypatch.setenv("CHAINLINK_DATA_STREAMS_API_SECRET", "api-secret")
    monkeypatch.setenv("CHAINLINK_BTC_USD_FEED_ID", feed_id)
    monkeypatch.setattr("src.price_signal.time.time", lambda: 1234567890.123)
    monkeypatch.setattr("src.price_signal.requests.get", fake_get)

    price = SpotPriceClient("CHAINLINK").btc_usd()

    assert price.price == Decimal("64000")
    assert price.source == "CHAINLINK"
    assert price.observed_at == 1234567890
    assert captured["url"].endswith(f"/api/v1/reports/latest?feedID={feed_id}")
    assert captured["headers"]["Authorization"] == "api-key"
    assert len(captured["headers"]["X-Authorization-Signature-SHA256"]) == 64


def test_clob_quotes_fetches_atomic_batch_and_maps_sides(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "up": {"BUY": "0.48", "SELL": "0.51"},
                "down": {"BUY": "0.49", "SELL": "0.52"},
            }

    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return Response()

    monkeypatch.setattr("src.polymarket.requests.post", fake_post)
    quotes = ClobDataClient("https://example.test", timeout=7).quotes(("up", "down"))

    assert quotes == (
        OrderBookQuote(bid=Decimal("0.48"), ask=Decimal("0.51")),
        OrderBookQuote(bid=Decimal("0.49"), ask=Decimal("0.52")),
    )
    assert calls == [
        (
            "https://example.test/prices",
            [
                {"token_id": "up", "side": "BUY"},
                {"token_id": "up", "side": "SELL"},
                {"token_id": "down", "side": "BUY"},
                {"token_id": "down", "side": "SELL"},
            ],
            7,
        )
    ]


def test_clob_books_fetches_atomic_depth_and_maps_token_order(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return [
                {
                    "asset_id": "down",
                    "timestamp": "123",
                    "bids": [{"price": "0.48", "size": "8"}],
                    "asks": [{"price": "0.53", "size": "6"}],
                    "min_order_size": "5",
                },
                {
                    "asset_id": "up",
                    "timestamp": "123",
                    "bids": [{"price": "0.47", "size": "7"}],
                    "asks": [
                        {"price": "0.52", "size": "4"},
                        {"price": "0.51", "size": "3"},
                    ],
                    "min_order_size": "5",
                },
            ]

    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return Response()

    monkeypatch.setattr("src.polymarket.requests.post", fake_post)
    up, down = ClobDataClient("https://example.test", timeout=7).books(("up", "down"))

    assert [level.price for level in up.asks] == [Decimal("0.51"), Decimal("0.52")]
    assert up.quote == OrderBookQuote(Decimal("0.47"), Decimal("0.51"))
    assert down.token_id == "down"
    assert calls == [
        (
            "https://example.test/books",
            [{"token_id": "up"}, {"token_id": "down"}],
            7,
        )
    ]


def test_executable_ask_depth_only_counts_liquidity_within_limit() -> None:
    book = OrderBookSnapshot(
        token_id="up",
        timestamp="1",
        bids=(),
        asks=(
            OrderBookLevel(price=Decimal("0.60"), size=Decimal("2")),
            OrderBookLevel(price=Decimal("0.61"), size=Decimal("3.5")),
            OrderBookLevel(price=Decimal("0.62"), size=Decimal("10")),
        ),
        minimum_order_size=Decimal("1"),
    )

    assert executable_ask_depth(book, Decimal("0.61")) == Decimal("5.5")


def test_btc_up_probability_moves_with_price() -> None:
    higher = btc_up_probability(Decimal("100"), Decimal("101"), Decimal("60"), Decimal("0.001"))
    lower = btc_up_probability(Decimal("100"), Decimal("99"), Decimal("60"), Decimal("0.001"))

    assert higher.probability_up > Decimal("0.5")
    assert lower.probability_up < Decimal("0.5")


def test_sigma_estimate_never_falls_below_long_run_floor() -> None:
    floor = Decimal("0.00005")

    sigma = estimate_sigma_per_sqrt_second(
        [Decimal("64000"), Decimal("64000.01"), Decimal("64000.02"), Decimal("64000.03")],
        Decimal("5"),
        floor,
    )

    assert sigma == floor


def test_sigma_estimate_uses_actual_sample_timing() -> None:
    prices = [Decimal("100"), Decimal("101"), Decimal("100")]
    floor = Decimal("0.000001")

    fixed_interval = estimate_sigma_per_sqrt_second(
        prices,
        Decimal("1"),
        floor,
    )
    actual_timing = estimate_sigma_per_sqrt_second(
        prices,
        Decimal("1"),
        floor,
        [100.0, 104.0, 108.0],
    )

    assert actual_timing < fixed_interval
    assert actual_timing > floor


def test_sigma_estimate_rejects_mismatched_sample_times() -> None:
    with pytest.raises(ValueError, match="sample_times"):
        estimate_sigma_per_sqrt_second(
            [Decimal("100"), Decimal("101"), Decimal("102")],
            Decimal("1"),
            Decimal("0.000001"),
            [100.0, 101.0],
        )


def test_adverse_jump_uses_dynamic_volatility_threshold() -> None:
    reset, adverse, threshold = adverse_jump_exceeds_dynamic_threshold(
        [Decimal("64000"), Decimal("64010")],
        "DOWN",
        Decimal("0.00005"),
        Decimal("5"),
        Decimal("1.25"),
        Decimal("3"),
    )
    stable, _, _ = adverse_jump_exceeds_dynamic_threshold(
        [Decimal("64000"), Decimal("64005")],
        "DOWN",
        Decimal("0.00005"),
        Decimal("5"),
        Decimal("1.25"),
        Decimal("3"),
    )

    assert reset is True
    assert adverse == Decimal("10")
    assert threshold > Decimal("8")
    assert stable is False


def test_theoretical_action_requires_edge() -> None:
    assert choose_theoretical_action(Decimal("0.60"), Decimal("0.52"), Decimal("0.48"), Decimal("0.06")).startswith("BUY_UP")
    assert choose_theoretical_action(Decimal("0.55"), Decimal("0.52"), Decimal("0.48"), Decimal("0.06")).startswith("SKIP")


def test_watch_updown_slug_helpers() -> None:
    assert slug_from_value("https://polymarket.com/zh/event/btc-updown-5m-1783685100") == "btc-updown-5m-1783685100"
    assert next_5m_slug("btc-updown-5m-1783685100") == "btc-updown-5m-1783685400"


def test_fair_value_edge_signal_buys_best_edge() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    signal = choose_fair_value_edge_signal(
        market=market,
        probability_up=Decimal("0.62"),
        up_quote=OrderBookQuote(bid=Decimal("0.50"), ask=Decimal("0.52")),
        down_quote=OrderBookQuote(bid=Decimal("0.46"), ask=Decimal("0.48")),
        seconds_to_end=Decimal("90"),
        decision_seconds_before_end=Decimal("120"),
        min_entry=Decimal("0.05"),
        max_entry=Decimal("0.95"),
        edge_threshold=Decimal("0.06"),
        max_spread=Decimal("0.05"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
    )

    assert signal is not None
    assert signal.side == "UP"
    assert signal.price == Decimal("0.52")


def test_open_060_buys_first_side_crossing_target() -> None:
    market = make_market(
        "Bitcoin Up or Down?",
        "btc-updown-5m-1",
        "c1",
        ("up", "down"),
        "0.01",
        False,
        Decimal("10"),
    )

    signal = choose_open_060_signal(
        market=market,
        up_quote=OrderBookQuote(bid=Decimal("0.60"), ask=Decimal("0.61")),
        down_quote=OrderBookQuote(bid=Decimal("0.39"), ask=Decimal("0.40")),
        seconds_to_end=Decimal("285"),
        previous_up_ask=Decimal("0.58"),
        previous_down_ask=Decimal("0.43"),
    )

    assert signal is not None
    assert signal.side == "UP"
    assert signal.price == Decimal("0.61")
    assert signal.reason.startswith("open_060 first_cross")


def test_open_060_uses_observed_ask_after_price_jumps_above_planned_entry() -> None:
    market = make_market(
        "Bitcoin Up or Down?",
        "btc-updown-5m-1",
        "c1",
        ("up", "down"),
        "0.01",
        False,
        Decimal("10"),
    )

    signal = choose_open_060_signal(
        market=market,
        up_quote=OrderBookQuote(bid=Decimal("0.61"), ask=Decimal("0.65")),
        down_quote=OrderBookQuote(bid=Decimal("0.34"), ask=Decimal("0.35")),
        seconds_to_end=Decimal("285"),
        previous_up_ask=Decimal("0.58"),
        previous_down_ask=Decimal("0.43"),
    )

    assert signal is not None
    assert signal.side == "UP"
    assert signal.price == Decimal("0.65")


def test_open_060_rejects_non_cross_and_late_cross() -> None:
    market = make_market(
        "Bitcoin Up or Down?",
        "btc-updown-5m-1",
        "c1",
        ("up", "down"),
        "0.01",
        False,
        Decimal("10"),
    )
    quotes = {
        "up_quote": OrderBookQuote(bid=Decimal("0.61"), ask=Decimal("0.62")),
        "down_quote": OrderBookQuote(bid=Decimal("0.38"), ask=Decimal("0.39")),
    }

    assert choose_open_060_signal(
        market,
        seconds_to_end=Decimal("285"),
        previous_up_ask=Decimal("0.61"),
        previous_down_ask=Decimal("0.39"),
        **quotes,
    ) is None
    assert choose_open_060_signal(
        market,
        seconds_to_end=Decimal("269"),
        previous_up_ask=Decimal("0.58"),
        previous_down_ask=Decimal("0.39"),
        **quotes,
    ) is None


def test_open_060_live_refresh_uses_current_actual_ask() -> None:
    market = make_market(
        "Bitcoin Up or Down?",
        "btc-updown-5m-1",
        "c1",
        ("up", "down"),
        "0.01",
        False,
        Decimal("10"),
    )

    signal = refresh_open_060_signal(
        market,
        "UP",
        OrderBookQuote(bid=Decimal("0.64"), ask=Decimal("0.65")),
        OrderBookQuote(bid=Decimal("0.34"), ask=Decimal("0.35")),
        Decimal("285"),
    )

    assert signal is not None
    assert signal.price == Decimal("0.65")
    assert "pre_submit_refresh=true" in signal.reason


def test_open_060_live_refresh_rejects_price_falling_below_target() -> None:
    market = make_market(
        "Bitcoin Up or Down?",
        "btc-updown-5m-1",
        "c1",
        ("up", "down"),
        "0.01",
        False,
        Decimal("10"),
    )

    assert refresh_open_060_signal(
        market,
        "UP",
        OrderBookQuote(bid=Decimal("0.58"), ask=Decimal("0.59")),
        OrderBookQuote(bid=Decimal("0.40"), ask=Decimal("0.41")),
        Decimal("285"),
    ) is None


def test_smart_score_accepts_strong_explainable_signal() -> None:
    market = make_market(
        "Bitcoin Up or Down?",
        "btc-updown-5m-1",
        "c1",
        ("up", "down"),
        "0.01",
        False,
        Decimal("10"),
    )
    signal = choose_smart_score_signal(
        market=market,
        probability_up=Decimal("0.82"),
        up_quote=OrderBookQuote(bid=Decimal("0.59"), ask=Decimal("0.60")),
        down_quote=OrderBookQuote(bid=Decimal("0.40"), ask=Decimal("0.41")),
        seconds_to_end=Decimal("70"),
        decision_seconds_before_end=Decimal("100"),
        min_seconds_before_end=Decimal("25"),
        min_entry=Decimal("0.50"),
        max_entry=Decimal("0.78"),
        edge_threshold=Decimal("0.02"),
        max_spread=Decimal("0.05"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
        recent_spot_prices=[Decimal("101"), Decimal("102"), Decimal("103")],
        start_price=Decimal("100"),
        up_ask_prices=[Decimal("0.58"), Decimal("0.59"), Decimal("0.60")],
        down_ask_prices=[Decimal("0.43"), Decimal("0.42"), Decimal("0.41")],
    )

    assert signal is not None
    assert signal.side == "UP"
    assert signal.price == Decimal("0.61")
    assert "smart_score total=" in signal.reason
    assert "components=edge:" in signal.reason
    components = signal.reason.split("components=", 1)[1]
    assert ",trend:" in components
    assert ",market:" in components
    assert ",stability:" in components
    assert ",timing:" in components
    assert "probability:" not in components
    assert "price:" not in components


def test_smart_score_rejects_weak_combined_evidence() -> None:
    market = make_market(
        "Bitcoin Up or Down?",
        "btc-updown-5m-1",
        "c1",
        ("up", "down"),
        "0.01",
        False,
        Decimal("10"),
    )
    signal = choose_smart_score_signal(
        market=market,
        probability_up=Decimal("0.55"),
        up_quote=OrderBookQuote(bid=Decimal("0.51"), ask=Decimal("0.52")),
        down_quote=OrderBookQuote(bid=Decimal("0.47"), ask=Decimal("0.48")),
        seconds_to_end=Decimal("70"),
        decision_seconds_before_end=Decimal("100"),
        min_seconds_before_end=Decimal("25"),
        min_entry=Decimal("0.45"),
        max_entry=Decimal("0.78"),
        edge_threshold=Decimal("0.02"),
        max_spread=Decimal("0.05"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
        recent_spot_prices=[Decimal("99"), Decimal("100"), Decimal("99")],
        start_price=Decimal("100"),
        up_ask_prices=[Decimal("0.47"), Decimal("0.55"), Decimal("0.52")],
        down_ask_prices=[Decimal("0.53"), Decimal("0.45"), Decimal("0.48")],
    )

    assert signal is None


def test_protective_hedge_allows_opposite_low_price_after_model_flip() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))

    signal = choose_protective_hedge_signal(
        market=market,
        primary_side="DOWN",
        probability_up=Decimal("0.65"),
        up_quote=OrderBookQuote(bid=Decimal("0.47"), ask=Decimal("0.48")),
        down_quote=OrderBookQuote(bid=Decimal("0.51"), ask=Decimal("0.52")),
        seconds_to_end=Decimal("66"),
        decision_seconds_before_end=Decimal("90"),
        min_seconds_before_end=Decimal("25"),
        max_entry=Decimal("0.78"),
        edge_threshold=Decimal("0.06"),
        min_win_probability=Decimal("0.62"),
        max_spread=Decimal("0.04"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
    )

    assert signal is not None
    assert signal.side == "UP"
    assert signal.price == Decimal("0.48")
    assert signal.reason.startswith("protective_hedge")


def test_market_reversal_hedge_uses_opposite_bid_inside_tail_window() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    quotes = {
        "up_quote": OrderBookQuote(bid=Decimal("0.70"), ask=Decimal("0.71")),
        "down_quote": OrderBookQuote(bid=Decimal("0.29"), ask=Decimal("0.30")),
    }

    signal = choose_market_reversal_hedge_signal(
        market=market,
        primary_side="DOWN",
        seconds_to_end=Decimal("12"),
        entry_start_seconds=Decimal("20"),
        entry_cutoff_seconds=Decimal("1"),
        reversal_bid_threshold=Decimal("0.65"),
        max_entry=Decimal("0.99"),
        max_spread=Decimal("0.04"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
        **quotes,
    )

    assert signal is not None
    assert signal.side == "UP"
    assert signal.price == Decimal("0.71")
    assert signal.reason.startswith("protective_market_reversal")
    assert choose_market_reversal_hedge_signal(
        market=market,
        primary_side="DOWN",
        seconds_to_end=Decimal("21"),
        entry_start_seconds=Decimal("20"),
        entry_cutoff_seconds=Decimal("1"),
        reversal_bid_threshold=Decimal("0.65"),
        max_entry=Decimal("0.99"),
        max_spread=Decimal("0.04"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
        **quotes,
    ) is None


def test_final_polling_switches_to_one_second_for_last_thirty_seconds() -> None:
    assert polling_interval_for_seconds_left(Decimal("31"), 5.0, 30, 1.0) == 5.0
    assert polling_interval_for_seconds_left(Decimal("30"), 5.0, 30, 1.0) == 1.0
    assert polling_interval_for_seconds_left(Decimal("1"), 5.0, 30, 1.0) == 1.0


def test_protective_hedge_must_reduce_portfolio_max_loss() -> None:
    improves = evaluate_protective_hedge_risk(
        primary_side="DOWN",
        primary_cost=Decimal("3.50"),
        primary_shares=Decimal("5"),
        hedge_price=Decimal("0.48"),
        hedge_shares=Decimal("5"),
        fee_rate=Decimal("0.07"),
    )
    worsens = evaluate_protective_hedge_risk(
        primary_side="UP",
        primary_cost=Decimal("0.90"),
        primary_shares=Decimal("1"),
        hedge_price=Decimal("0.78"),
        hedge_shares=Decimal("5"),
        fee_rate=Decimal("0.07"),
    )

    assert improves.reduces_max_loss is True
    assert improves.max_loss_after < improves.max_loss_before
    assert worsens.reduces_max_loss is False
    assert worsens.max_loss_after > worsens.max_loss_before


def test_response_fill_amounts_prefers_actual_match_amounts() -> None:
    cost, shares = response_fill_amounts(
        {"makingAmount": "2.499999", "takingAmount": "7.142856"},
        Decimal("0.50"),
        Decimal("5"),
    )

    assert cost == Decimal("2.499999")
    assert shares == Decimal("7.142856")


def test_probability_shrinkage_moves_tail_confidence_toward_even() -> None:
    assert shrink_probability_toward_even(Decimal("0.90"), Decimal("0.40")) == Decimal("0.660")
    assert shrink_probability_toward_even(Decimal("0.50"), Decimal("0.40")) == Decimal("0.500")


def test_fair_value_edge_uses_calibrated_probability() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    base = {
        "market": market,
        "up_quote": OrderBookQuote(bid=Decimal("0.69"), ask=Decimal("0.70")),
        "down_quote": OrderBookQuote(bid=Decimal("0.29"), ask=Decimal("0.30")),
        "seconds_to_end": Decimal("60"),
        "decision_seconds_before_end": Decimal("90"),
        "min_entry": Decimal("0.45"),
        "max_entry": Decimal("0.75"),
        "edge_threshold": Decimal("0.06"),
        "max_spread": Decimal("0.04"),
        "min_ask_sum": Decimal("0.90"),
        "max_ask_sum": Decimal("1.10"),
        "min_win_probability": Decimal("0.62"),
    }

    assert choose_fair_value_edge_signal(probability_up=Decimal("0.86"), **base) is not None
    assert choose_fair_value_edge_signal(
        probability_up=Decimal("0.86"),
        probability_shrinkage=Decimal("0.40"),
        **base,
    ) is None

    calibrated = choose_fair_value_edge_signal(
        probability_up=Decimal("0.95"),
        probability_shrinkage=Decimal("0.40"),
        **{**base, "up_quote": OrderBookQuote(bid=Decimal("0.49"), ask=Decimal("0.50")), "down_quote": OrderBookQuote(bid=Decimal("0.49"), ask=Decimal("0.50"))},
    )
    assert calibrated is not None
    assert "p_up=0.6800" in calibrated.reason
    assert "raw_p_up=0.9500" in calibrated.reason
    assert "shrinkage=0.40" in calibrated.reason


def test_fair_value_edge_signal_rejects_wide_spread() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    signal = choose_fair_value_edge_signal(
        market=market,
        probability_up=Decimal("0.70"),
        up_quote=OrderBookQuote(bid=Decimal("0.40"), ask=Decimal("0.55")),
        down_quote=OrderBookQuote(bid=Decimal("0.44"), ask=Decimal("0.45")),
        seconds_to_end=Decimal("90"),
        decision_seconds_before_end=Decimal("120"),
        min_entry=Decimal("0.05"),
        max_entry=Decimal("0.95"),
        edge_threshold=Decimal("0.06"),
        max_spread=Decimal("0.05"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
    )

    assert signal is None


def test_fair_value_edge_signal_requires_minimum_win_probability() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    signal = choose_fair_value_edge_signal(
        market=market,
        probability_up=Decimal("0.60"),
        up_quote=OrderBookQuote(bid=Decimal("0.48"), ask=Decimal("0.50")),
        down_quote=OrderBookQuote(bid=Decimal("0.48"), ask=Decimal("0.50")),
        seconds_to_end=Decimal("70"),
        decision_seconds_before_end=Decimal("90"),
        min_entry=Decimal("0.50"),
        max_entry=Decimal("0.78"),
        edge_threshold=Decimal("0.06"),
        max_spread=Decimal("0.04"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
        min_seconds_before_end=Decimal("25"),
        min_win_probability=Decimal("0.62"),
    )

    assert signal is None


def test_50_cent_entry_uses_strict_probability_tier_and_spot_samples() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    rejected = choose_fair_value_edge_signal(
        market=market,
        probability_up=Decimal("0.60"),
        up_quote=OrderBookQuote(bid=Decimal("0.49"), ask=Decimal("0.50")),
        down_quote=OrderBookQuote(bid=Decimal("0.49"), ask=Decimal("0.50")),
        seconds_to_end=Decimal("70"),
        decision_seconds_before_end=Decimal("90"),
        min_entry=Decimal("0.50"),
        max_entry=Decimal("0.78"),
        edge_threshold=Decimal("0.02"),
        max_spread=Decimal("0.04"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
        min_win_probability=Decimal("0.55"),
        low_entry_cutoff=Decimal("0.55"),
    )
    accepted = choose_fair_value_edge_signal(
        market=market,
        probability_up=Decimal("0.63"),
        up_quote=OrderBookQuote(bid=Decimal("0.49"), ask=Decimal("0.50")),
        down_quote=OrderBookQuote(bid=Decimal("0.49"), ask=Decimal("0.50")),
        seconds_to_end=Decimal("70"),
        decision_seconds_before_end=Decimal("90"),
        min_entry=Decimal("0.50"),
        max_entry=Decimal("0.78"),
        edge_threshold=Decimal("0.02"),
        max_spread=Decimal("0.04"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
        min_win_probability=Decimal("0.55"),
        low_entry_cutoff=Decimal("0.55"),
        recent_spot_prices=[Decimal("100.5"), Decimal("101"), Decimal("102")],
        start_price=Decimal("100"),
    )

    assert rejected is None
    assert accepted is not None
    assert accepted.price == Decimal("0.50")
    assert "required_probability=0.6100" in accepted.reason


def test_recent_spot_samples_require_same_side_and_supporting_net_move() -> None:
    start = Decimal("100")

    assert recent_spot_samples_support_side(
        [Decimal("99"), Decimal("101"), Decimal("100.5"), Decimal("102")],
        start,
        "UP",
        3,
    ) is True
    assert recent_spot_samples_support_side(
        [Decimal("101"), Decimal("103"), Decimal("102"), Decimal("101.5")],
        start,
        "UP",
        3,
    ) is False
    assert recent_spot_samples_support_side(
        [Decimal("101"), Decimal("99"), Decimal("99.5"), Decimal("98")],
        start,
        "DOWN",
        3,
    ) is True


def test_recent_spot_samples_allow_configured_pullback_without_crossing_open() -> None:
    start = Decimal("100")

    up_prices = [Decimal("101"), Decimal("100.8"), Decimal("100.3"), Decimal("100.1")]
    assert recent_spot_samples_support_side(up_prices, start, "UP", 3) is False
    assert recent_spot_samples_support_side(
        up_prices,
        start,
        "UP",
        3,
        Decimal("1.00"),
    ) is True

    down_prices = [Decimal("99"), Decimal("99.2"), Decimal("99.7"), Decimal("99.9")]
    assert recent_spot_samples_support_side(down_prices, start, "DOWN", 3) is False
    assert recent_spot_samples_support_side(
        down_prices,
        start,
        "DOWN",
        3,
        Decimal("1.00"),
    ) is True

    assert recent_spot_samples_support_side(
        [Decimal("100.5"), Decimal("99.9"), Decimal("100.1")],
        start,
        "UP",
        3,
        Decimal("1.00"),
    ) is False
    assert recent_spot_samples_support_side(
        [Decimal("106"), Decimal("116"), Decimal("113")],
        start,
        "UP",
        3,
        Decimal("1.00"),
    ) is False


def test_pullback_tolerance_uses_larger_fixed_or_percentage_value() -> None:
    start = Decimal("66000")

    assert effective_pullback_tolerance(Decimal("1.00"), Decimal("8"), Decimal("25")) == Decimal("2")
    assert effective_pullback_tolerance(Decimal("1.00"), Decimal("2"), Decimal("25")) == Decimal("1.00")
    assert recent_spot_samples_support_side(
        [Decimal("66008"), Decimal("66012"), Decimal("66009")],
        start,
        "UP",
        3,
        Decimal("1.00"),
        Decimal("25"),
    ) is True
    assert recent_spot_samples_support_side(
        [Decimal("66008"), Decimal("66012"), Decimal("66008.5")],
        start,
        "UP",
        3,
        Decimal("1.00"),
        Decimal("25"),
    ) is False


def test_low_entry_requires_61_percent_and_three_supporting_samples() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    base = {
        "market": market,
        "up_quote": OrderBookQuote(bid=Decimal("0.46"), ask=Decimal("0.47")),
        "down_quote": OrderBookQuote(bid=Decimal("0.52"), ask=Decimal("0.53")),
        "seconds_to_end": Decimal("70"),
        "decision_seconds_before_end": Decimal("90"),
        "min_entry": Decimal("0.45"),
        "max_entry": Decimal("0.75"),
        "edge_threshold": Decimal("0.02"),
        "max_spread": Decimal("0.04"),
        "min_ask_sum": Decimal("0.90"),
        "max_ask_sum": Decimal("1.10"),
        "min_win_probability": Decimal("0.55"),
        "recent_spot_prices": [Decimal("101"), Decimal("100.5"), Decimal("102")],
        "start_price": Decimal("100"),
    }

    accepted = choose_fair_value_edge_signal(probability_up=Decimal("0.63"), **base)
    low_probability = choose_fair_value_edge_signal(probability_up=Decimal("0.60"), **base)
    reversed_samples = choose_fair_value_edge_signal(
        probability_up=Decimal("0.63"),
        **{
            **base,
            "recent_spot_prices": [Decimal("102"), Decimal("101"), Decimal("100.5")],
        },
    )

    assert accepted is not None
    assert accepted.side == "UP"
    assert "required_probability=0.6100" in accepted.reason
    assert low_probability is None
    assert reversed_samples is None


def test_75_cent_entry_requires_existing_dynamic_high_price_edge() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    signal = choose_fair_value_edge_signal(
        market=market,
        probability_up=Decimal("0.86"),
        up_quote=OrderBookQuote(bid=Decimal("0.74"), ask=Decimal("0.75")),
        down_quote=OrderBookQuote(bid=Decimal("0.24"), ask=Decimal("0.25")),
        seconds_to_end=Decimal("60"),
        decision_seconds_before_end=Decimal("90"),
        min_entry=Decimal("0.45"),
        max_entry=Decimal("0.75"),
        edge_threshold=Decimal("0.06"),
        max_spread=Decimal("0.04"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
        min_win_probability=Decimal("0.62"),
    )

    assert signal is not None
    assert signal.price == Decimal("0.75")
    assert "required_edge=0.0850" in signal.reason


def test_late_favorite_accepts_fee_adjusted_high_confidence_signal() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    signal = choose_late_favorite_signal(
        market=market,
        probability_up=Decimal("0.84"),
        up_quote=OrderBookQuote(bid=Decimal("0.74"), ask=Decimal("0.75")),
        down_quote=OrderBookQuote(bid=Decimal("0.24"), ask=Decimal("0.25")),
        seconds_to_end=Decimal("30"),
        recent_spot_prices=[
            Decimal("100.04"),
            Decimal("100.05"),
            Decimal("100.06"),
            Decimal("100.07"),
            Decimal("100.08"),
            Decimal("100.09"),
        ],
        start_price=Decimal("100"),
    )

    assert signal is not None
    assert signal.side == "UP"
    assert signal.price == Decimal("0.75")
    assert "required_probability=0.8231" in signal.reason
    assert "expected_roi=" in signal.reason
    assert "lead_bps=" in signal.reason
    assert "required_lead_bps=3.00" in signal.reason
    assert "pullback_ratio=0.000" in signal.reason
    assert "fee_per_share=0.0131" in signal.reason


def test_late_favorite_rejects_weak_probability_time_and_reversal() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    base = {
        "market": market,
        "up_quote": OrderBookQuote(bid=Decimal("0.74"), ask=Decimal("0.75")),
        "down_quote": OrderBookQuote(bid=Decimal("0.24"), ask=Decimal("0.25")),
        "recent_spot_prices": [
            Decimal("100.04"),
            Decimal("100.05"),
            Decimal("100.06"),
            Decimal("100.07"),
            Decimal("100.08"),
            Decimal("100.09"),
        ],
        "start_price": Decimal("100"),
    }

    assert choose_late_favorite_signal(
        probability_up=Decimal("0.79"),
        seconds_to_end=Decimal("30"),
        **base,
    ) is None
    assert choose_late_favorite_signal(
        probability_up=Decimal("0.84"),
        seconds_to_end=Decimal("31"),
        **base,
    ) is None
    assert choose_late_favorite_signal(
        probability_up=Decimal("0.84"),
        seconds_to_end=Decimal("30"),
        **{
            **base,
            "recent_spot_prices": [
                Decimal("100.04"),
                Decimal("100.05"),
                Decimal("100.06"),
                Decimal("100.07"),
                Decimal("100.08"),
                Decimal("99.99"),
            ],
        },
    ) is None


def test_late_favorite_rejects_wide_spread() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    signal = choose_late_favorite_signal(
        market=market,
        probability_up=Decimal("0.90"),
        up_quote=OrderBookQuote(bid=Decimal("0.71"), ask=Decimal("0.75")),
        down_quote=OrderBookQuote(bid=Decimal("0.24"), ask=Decimal("0.25")),
        seconds_to_end=Decimal("30"),
        recent_spot_prices=[
            Decimal("100.04"),
            Decimal("100.05"),
            Decimal("100.06"),
            Decimal("100.07"),
            Decimal("100.08"),
            Decimal("100.09"),
        ],
        start_price=Decimal("100"),
    )

    assert signal is None


def test_late_spot_buffer_rejects_small_lead_and_pullback() -> None:
    assert late_spot_buffer_metrics(
        [Decimal("10001"), Decimal("10001.2"), Decimal("10001.4")],
        Decimal("10000"),
        "UP",
        3,
    ) == (Decimal("1.40000"), Decimal("0"))

    market = make_market(
        "Bitcoin Up or Down?",
        "btc-updown-5m-1",
        "c1",
        ("up", "down"),
        "0.01",
        False,
        Decimal("10"),
    )
    base = {
        "market": market,
        "probability_up": Decimal("0.95"),
        "up_quote": OrderBookQuote(bid=Decimal("0.83"), ask=Decimal("0.84")),
        "down_quote": OrderBookQuote(bid=Decimal("0.14"), ask=Decimal("0.15")),
        "seconds_to_end": Decimal("30"),
        "start_price": Decimal("10000"),
    }
    assert choose_late_favorite_signal(
        recent_spot_prices=[
            Decimal("10001"),
            Decimal("10001.1"),
            Decimal("10001.2"),
            Decimal("10001.3"),
            Decimal("10001.35"),
            Decimal("10001.4"),
        ],
        **base,
    ) is None
    assert choose_late_favorite_signal(
        recent_spot_prices=[
            Decimal("10002"),
            Decimal("10003"),
            Decimal("10004"),
            Decimal("10003.8"),
            Decimal("10003.4"),
            Decimal("10003"),
        ],
        **base,
    ) is None


def test_late_favorite_rejects_dynamic_volatility_buffer_and_recent_cross() -> None:
    market = make_market(
        "Bitcoin Up or Down?",
        "btc-updown-5m-1",
        "c1",
        ("up", "down"),
        "0.01",
        False,
        Decimal("10"),
    )
    base = {
        "market": market,
        "probability_up": Decimal("0.95"),
        "up_quote": OrderBookQuote(bid=Decimal("0.79"), ask=Decimal("0.80")),
        "down_quote": OrderBookQuote(bid=Decimal("0.19"), ask=Decimal("0.20")),
        "seconds_to_end": Decimal("20"),
        "start_price": Decimal("10000"),
    }

    assert choose_late_favorite_signal(
        recent_spot_prices=[
            Decimal("10003"),
            Decimal("10003.1"),
            Decimal("10003.2"),
            Decimal("10003.3"),
            Decimal("10003.4"),
            Decimal("10003.5"),
        ],
        sigma_per_sqrt_second=Decimal("0.00010"),
        **base,
    ) is None
    assert choose_late_favorite_signal(
        recent_spot_prices=[
            Decimal("9999.9"),
            Decimal("10004"),
            Decimal("10005"),
            Decimal("10006"),
            Decimal("10007"),
            Decimal("10008"),
        ],
        **base,
    ) is None


def test_late_favorite_requires_fee_adjusted_expected_roi() -> None:
    market = make_market(
        "Bitcoin Up or Down?",
        "btc-updown-5m-1",
        "c1",
        ("up", "down"),
        "0.01",
        False,
        Decimal("10"),
    )
    signal = choose_late_favorite_signal(
        market=market,
        probability_up=Decimal("0.91"),
        up_quote=OrderBookQuote(bid=Decimal("0.89"), ask=Decimal("0.90")),
        down_quote=OrderBookQuote(bid=Decimal("0.09"), ask=Decimal("0.10")),
        seconds_to_end=Decimal("30"),
        recent_spot_prices=[Decimal("10002"), Decimal("10003"), Decimal("10004")],
        start_price=Decimal("10000"),
        max_entry=Decimal("0.92"),
        no_cross_samples=3,
    )

    assert signal is None


def test_fair_value_reserves_one_extra_protection_slot() -> None:
    assert strategy_trade_limit("fair_value_edge", 5) == 6
    assert strategy_trade_limit("late_070", 5) == 6
    assert strategy_trade_limit("smart_score", 5) == 1
    assert strategy_trade_limit("open_060", 5) == 1


def test_late_070_uses_first_primary_confirmation_only() -> None:
    assert primary_signal_confirmation_count("late_070", 2) == 1
    assert primary_signal_confirmation_count("fair_value_edge", 2) == 2


def test_loss_pause_consumes_each_full_window_without_off_by_one() -> None:
    remaining = 3
    states = []
    for _ in range(4):
        active, remaining = consume_pause_window(remaining)
        states.append(active)

    assert states == [True, True, True, False]


def test_disabled_loss_pause_does_not_accumulate_streak() -> None:
    paper_positions = []
    signal = AutoTradeSignal("UP", "up-token", Decimal("0.50"), "test")
    open_paper_position(paper_positions, Decimal("20"), "slug-a", signal, Decimal("1"))
    paper_positions[0].settled = True
    paper_positions[0].profit = Decimal("-1")

    streak, pause = account_new_paper_settlements(
        paper_positions,
        consecutive_losses=4,
        max_consecutive_losses=0,
        pause_windows_after_losses=0,
    )

    assert streak == 0
    assert pause == 0


def test_fair_value_edge_signal_rejects_late_entry() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    signal = choose_fair_value_edge_signal(
        market=market,
        probability_up=Decimal("0.75"),
        up_quote=OrderBookQuote(bid=Decimal("0.58"), ask=Decimal("0.60")),
        down_quote=OrderBookQuote(bid=Decimal("0.38"), ask=Decimal("0.40")),
        seconds_to_end=Decimal("20"),
        decision_seconds_before_end=Decimal("90"),
        min_entry=Decimal("0.50"),
        max_entry=Decimal("0.78"),
        edge_threshold=Decimal("0.06"),
        max_spread=Decimal("0.04"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
        min_seconds_before_end=Decimal("25"),
        min_win_probability=Decimal("0.62"),
    )

    assert signal is None


def test_fair_value_edge_signal_demands_more_edge_for_expensive_entry() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    signal = choose_fair_value_edge_signal(
        market=market,
        probability_up=Decimal("0.78"),
        up_quote=OrderBookQuote(bid=Decimal("0.69"), ask=Decimal("0.70")),
        down_quote=OrderBookQuote(bid=Decimal("0.29"), ask=Decimal("0.30")),
        seconds_to_end=Decimal("35"),
        decision_seconds_before_end=Decimal("90"),
        min_entry=Decimal("0.50"),
        max_entry=Decimal("0.78"),
        edge_threshold=Decimal("0.06"),
        max_spread=Decimal("0.04"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
        min_seconds_before_end=Decimal("25"),
        min_win_probability=Decimal("0.62"),
    )

    assert signal is None


def test_quote_sanity_rejects_crossed_market() -> None:
    ok, reason = quotes_pass_sanity_checks(
        OrderBookQuote(bid=Decimal("0.54"), ask=Decimal("0.52")),
        OrderBookQuote(bid=Decimal("0.46"), ask=Decimal("0.48")),
        max_spread=Decimal("0.05"),
        min_ask_sum=Decimal("0.90"),
        max_ask_sum=Decimal("1.10"),
    )

    assert ok is False
    assert "bid" in reason


def test_account_new_paper_settlements_triggers_pause() -> None:
    positions = [
        AutoTradeSignal("UP", "up-token", Decimal("0.50"), "test"),
        AutoTradeSignal("DOWN", "down-token", Decimal("0.50"), "test"),
    ]
    paper_positions = []
    bankroll = open_paper_position(paper_positions, Decimal("20"), "slug-a", positions[0], Decimal("1"))
    open_paper_position(paper_positions, bankroll, "slug-b", positions[1], Decimal("1"))
    for position in paper_positions:
        position.settled = True
        position.profit = Decimal("-1")

    consecutive_losses, pause_windows = account_new_paper_settlements(
        paper_positions,
        consecutive_losses=0,
        max_consecutive_losses=2,
        pause_windows_after_losses=3,
    )

    assert consecutive_losses == 0
    assert pause_windows == 3
    assert all(position.accounted for position in paper_positions)


def test_backtest_helpers() -> None:
    assert previous_slugs("btc-updown-5m-1783685100", 3) == [
        "btc-updown-5m-1783685100",
        "btc-updown-5m-1783684800",
        "btc-updown-5m-1783684500",
    ]
    assert winning_side('["1", "0"]') == "UP"
    assert winning_side('["0", "1"]') == "DOWN"
    points = [PricePoint(10, Decimal("0.4")), PricePoint(20, Decimal("0.6"))]
    assert price_at_or_before(points, 19) == Decimal("0.4")
    assert price_at_or_before(points, 20) == Decimal("0.6")


def test_open_paper_position_debits_bankroll() -> None:
    positions = []
    signal = AutoTradeSignal("UP", "up-token", Decimal("0.50"), "test")

    bankroll = open_paper_position(positions, Decimal("20"), "slug", signal, Decimal("1"))

    assert bankroll == Decimal("19")
    assert len(positions) == 1
    assert positions[0].shares == Decimal("2")


def test_settle_paper_positions_winner(monkeypatch) -> None:
    positions = []
    signal = AutoTradeSignal("UP", "up-token", Decimal("0.50"), "test")
    bankroll = open_paper_position(positions, Decimal("20"), "slug", signal, Decimal("1"))

    monkeypatch.setattr("src.watch_updown.fetch_winner", lambda slug: "UP")
    bankroll = settle_paper_positions(positions, "slug", bankroll)

    assert bankroll == Decimal("21")
    assert positions[0].settled is True


def test_settle_all_paper_positions(monkeypatch) -> None:
    positions = []
    up = AutoTradeSignal("UP", "up-token", Decimal("0.50"), "test")
    down = AutoTradeSignal("DOWN", "down-token", Decimal("0.25"), "test")
    bankroll = open_paper_position(positions, Decimal("20"), "slug-a", up, Decimal("1"))
    bankroll = open_paper_position(positions, bankroll, "slug-b", down, Decimal("1"))

    monkeypatch.setattr("src.watch_updown.fetch_winner", lambda slug: {"slug-a": "DOWN", "slug-b": "DOWN"}[slug])
    bankroll = settle_all_paper_positions(positions, bankroll)

    assert bankroll == Decimal("22")
    assert all(position.settled for position in positions)
