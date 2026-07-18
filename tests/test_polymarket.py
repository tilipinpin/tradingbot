from decimal import Decimal
import json
import time

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
    OrderBookQuote,
    OrderBookSnapshot,
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
    account_new_paper_settlements,
    adverse_jump_exceeds_dynamic_threshold,
    apply_split_maker_exit,
    consume_pause_window,
    evaluate_protective_hedge_risk,
    choose_fair_value_edge_signal,
    choose_protective_hedge_signal,
    choose_late_favorite_signal,
    choose_maker_momentum_signal,
    evaluate_maker_momentum_signal,
    late_spot_buffer_metrics,
    live_order_limit_reached,
    live_response_is_matched,
    live_session_should_continue,
    maker_quote_crossed,
    merge_split_maker_inventory,
    window_trade_count_after_attempt,
    open_paper_position,
    open_split_maker_cycle,
    plan_split_maker_quotes,
    quotes_pass_sanity_checks,
    response_fill_amounts,
    recent_spot_samples_support_side,
    record_split_maker_fill,
    settle_all_paper_positions,
    settle_paper_positions,
    settle_split_maker_cycles,
    split_maker_best_exit,
    shrink_probability_toward_even,
    strategy_trade_limit,
    AutoTradeSignal,
    SplitMakerCycle,
    next_5m_slug,
    slug_from_value,
    start_capture_is_too_late,
    parse_args as parse_watch_args,
)


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


def test_split_maker_quote_plan_targets_pair_sum_and_requires_resting_time() -> None:
    up_book = OrderBookSnapshot(
        "up",
        "1",
        (OrderBookLevel(Decimal("0.49"), Decimal("10")),),
        (OrderBookLevel(Decimal("0.52"), Decimal("10")),),
        Decimal("5"),
    )
    down_book = OrderBookSnapshot(
        "down",
        "1",
        (OrderBookLevel(Decimal("0.49"), Decimal("10")),),
        (OrderBookLevel(Decimal("0.52"), Decimal("10")),),
        Decimal("5"),
    )

    plan = plan_split_maker_quotes(
        Decimal("0.50"),
        up_book,
        down_book,
        Decimal("1.02"),
        Decimal("0.01"),
    )

    assert plan is not None
    assert plan.up_price == Decimal("0.51")
    assert plan.down_price == Decimal("0.51")
    assert maker_quote_crossed(up_book, Decimal("0.49"), 100.0, 103.9, 4) is False
    assert maker_quote_crossed(up_book, Decimal("0.49"), 100.0, 104.0, 4) is True


def test_split_maker_both_fills_lock_pair_sum_profit() -> None:
    bankroll, cycle = open_split_maker_cycle(Decimal("20"), "btc-updown-5m-100", Decimal("5"))
    assert cycle is not None

    bankroll += record_split_maker_fill(cycle, "UP", Decimal("0.51"))
    bankroll += record_split_maker_fill(cycle, "DOWN", Decimal("0.51"))

    assert cycle.closed is True
    assert cycle.close_reason == "both_maker_quotes_filled"
    assert cycle.cash_flow == Decimal("0.10")
    assert bankroll == Decimal("20.10")


def test_split_maker_exit_chooses_better_inventory_recovery() -> None:
    cycle = SplitMakerCycle(
        slug="btc-updown-5m-100",
        shares=Decimal("5"),
        cash_flow=Decimal("-2.35"),
        inventory_up=Decimal("0"),
        inventory_down=Decimal("5"),
        up_sold_price=Decimal("0.53"),
    )
    up_book = OrderBookSnapshot(
        "up",
        "1",
        (OrderBookLevel(Decimal("0.52"), Decimal("5")),),
        (OrderBookLevel(Decimal("0.54"), Decimal("5")),),
        Decimal("5"),
    )
    down_book = OrderBookSnapshot(
        "down",
        "1",
        (OrderBookLevel(Decimal("0.48"), Decimal("5")),),
        (OrderBookLevel(Decimal("0.50"), Decimal("5")),),
        Decimal("5"),
    )

    exit_plan = split_maker_best_exit(cycle, up_book, down_book, Decimal("0.07"))
    assert exit_plan is not None
    assert exit_plan.action == "sell_remaining_inventory"
    cash_delta = apply_split_maker_exit(cycle, exit_plan)

    assert cycle.closed is True
    assert cash_delta == Decimal("2.312640")
    assert cycle.cash_flow == Decimal("-0.037360")


def test_split_maker_cancel_merges_inventory_and_settlement_closes_orphan(monkeypatch) -> None:
    bankroll, untouched = open_split_maker_cycle(Decimal("20"), "btc-updown-5m-100", Decimal("5"))
    assert untouched is not None
    merged = merge_split_maker_inventory(untouched)
    bankroll += merged
    assert bankroll == Decimal("20")
    assert untouched.cash_flow == 0

    bankroll, orphan = open_split_maker_cycle(Decimal("20"), "btc-updown-5m-100", Decimal("5"))
    assert orphan is not None
    bankroll += record_split_maker_fill(orphan, "UP", Decimal("0.53"))
    monkeypatch.setattr("src.watch_updown.fetch_winner", lambda slug: "DOWN")
    bankroll = settle_split_maker_cycles([orphan], bankroll, now_ts=500)

    assert orphan.closed is True
    assert orphan.close_reason == "inventory_settlement"
    assert orphan.cash_flow == Decimal("2.65")
    assert bankroll == Decimal("22.65")


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

    assert args.max_live_orders == 0
    assert args.max_trades == 2
    assert args.duration == 0
    assert args.min_entry == "0.55"
    assert args.trend_confirmation_samples == 3
    assert args.hedge_signal_confirmations == 2
    assert args.hedge_min_win_probability == "0.62"
    assert args.hedge_fee_rate == "0.07"
    assert args.max_entry == "0.78"
    assert args.max_live_notional == "3.75"
    assert args.low_entry_cutoff == "0.50"
    assert args.low_entry_min_win_probability == "0.68"
    assert args.low_entry_confirmation_samples == 3
    assert args.probability_shrinkage == "1.00"
    assert args.maker_shares == "5"
    assert args.maker_target_pair_sum == "1.02"
    assert args.maker_fee_rate == "0.07"
    assert args.maker_start_delay_seconds == 10
    assert args.maker_min_rest_seconds == 4
    assert args.maker_reprice_ticks == 2
    assert args.maker_unpaired_timeout_seconds == 10
    assert args.maker_cancel_seconds == 60
    assert args.maker_force_exit_seconds == 45
    assert args.maker_max_inventory_loss_rate == "0.01"
    assert args.momentum_target_pair_sum == "1.02"
    assert args.momentum_start_delay_seconds == 10
    assert args.momentum_min_rest_seconds == 4
    assert args.momentum_reprice_ticks == 2
    assert args.momentum_confirmation_samples == 1
    assert args.momentum_trigger_timeout_seconds == 8
    assert args.momentum_min_seconds_before_end == 30
    assert args.momentum_min_entry == "0.60"
    assert args.momentum_max_entry == "0.88"
    assert args.momentum_min_probability == "0.55"
    assert args.momentum_flow_probability_boost == "0.10"
    assert args.momentum_min_expected_roi == "0.03"
    assert args.momentum_min_lead_bps == "0.50"
    assert args.momentum_strong_expected_roi == "0.04"
    assert args.momentum_strong_lead_bps == "2.00"
    assert args.momentum_spot_samples == 3
    assert args.momentum_max_chase == "0.06"
    assert args.momentum_fee_rate == "0.07"
    assert args.momentum_max_spread == "0.02"
    assert args.momentum_min_ask_sum == "0.97"
    assert args.momentum_max_ask_sum == "1.03"
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


def test_live_response_requires_conclusive_match() -> None:
    assert live_response_is_matched({"success": True, "status": "matched", "orderID": "0x1"}) is True
    assert live_response_is_matched({"success": True, "status": "live", "orderID": "0x1"}) is False
    assert live_response_is_matched({"success": False, "status": "matched", "orderID": "0x1"}) is False


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


def test_start_capture_rejects_fresh_price_after_deadline() -> None:
    assert start_capture_is_too_late(Decimal("-16"), Decimal("15")) is True
    assert start_capture_is_too_late(Decimal("-15"), Decimal("15")) is False


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


def test_50_cent_entry_uses_normal_probability_tier_without_spot_samples() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    signal = choose_fair_value_edge_signal(
        market=market,
        probability_up=Decimal("0.62"),
        up_quote=OrderBookQuote(bid=Decimal("0.49"), ask=Decimal("0.50")),
        down_quote=OrderBookQuote(bid=Decimal("0.49"), ask=Decimal("0.50")),
        seconds_to_end=Decimal("70"),
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
    assert signal.price == Decimal("0.50")
    assert "required_probability=0.6200" in signal.reason


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


def test_low_entry_requires_68_percent_and_three_supporting_samples() -> None:
    market = make_market("Bitcoin Up or Down?", "btc-updown-5m-1", "c1", ("up", "down"), "0.01", False, Decimal("10"))
    base = {
        "market": market,
        "up_quote": OrderBookQuote(bid=Decimal("0.46"), ask=Decimal("0.47")),
        "down_quote": OrderBookQuote(bid=Decimal("0.52"), ask=Decimal("0.53")),
        "seconds_to_end": Decimal("70"),
        "decision_seconds_before_end": Decimal("90"),
        "min_entry": Decimal("0.45"),
        "max_entry": Decimal("0.75"),
        "edge_threshold": Decimal("0.06"),
        "max_spread": Decimal("0.04"),
        "min_ask_sum": Decimal("0.90"),
        "max_ask_sum": Decimal("1.10"),
        "min_win_probability": Decimal("0.62"),
        "recent_spot_prices": [Decimal("101"), Decimal("100.5"), Decimal("102")],
        "start_price": Decimal("100"),
    }

    accepted = choose_fair_value_edge_signal(probability_up=Decimal("0.70"), **base)
    low_probability = choose_fair_value_edge_signal(probability_up=Decimal("0.67"), **base)
    reversed_samples = choose_fair_value_edge_signal(
        probability_up=Decimal("0.70"),
        **{
            **base,
            "recent_spot_prices": [Decimal("102"), Decimal("101"), Decimal("100.5")],
        },
    )

    assert accepted is not None
    assert accepted.side == "UP"
    assert "required_probability=0.6800" in accepted.reason
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


def test_maker_momentum_accepts_aligned_favorite_after_virtual_fill() -> None:
    market = make_market(
        "Bitcoin Up or Down?",
        "btc-updown-5m-1",
        "c1",
        ("up", "down"),
        "0.01",
        False,
        Decimal("10"),
    )
    signal = choose_maker_momentum_signal(
        market=market,
        side="UP",
        trigger_price=Decimal("0.68"),
        probability_up=Decimal("0.66"),
        up_quote=OrderBookQuote(bid=Decimal("0.70"), ask=Decimal("0.71")),
        down_quote=OrderBookQuote(bid=Decimal("0.29"), ask=Decimal("0.30")),
        seconds_to_end=Decimal("200"),
        recent_spot_prices=[Decimal("100.01"), Decimal("100.02"), Decimal("100.03")],
        start_price=Decimal("100"),
    )

    assert signal is not None
    assert signal.side == "UP"
    assert signal.price == Decimal("0.71")
    assert "flow_adjusted_probability=0.7600" in signal.reason


def test_maker_momentum_rejects_underdog_opposite_spot_and_excessive_chase() -> None:
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
        "side": "UP",
        "trigger_price": Decimal("0.68"),
        "probability_up": Decimal("0.75"),
        "up_quote": OrderBookQuote(bid=Decimal("0.70"), ask=Decimal("0.71")),
        "down_quote": OrderBookQuote(bid=Decimal("0.29"), ask=Decimal("0.30")),
        "seconds_to_end": Decimal("200"),
        "recent_spot_prices": [Decimal("100.01"), Decimal("100.02"), Decimal("100.03")],
        "start_price": Decimal("100"),
    }

    assert choose_maker_momentum_signal(
        **{
            **base,
            "up_quote": OrderBookQuote(bid=Decimal("0.44"), ask=Decimal("0.45")),
            "down_quote": OrderBookQuote(bid=Decimal("0.55"), ask=Decimal("0.56")),
        }
    ) is None
    assert choose_maker_momentum_signal(
        **{
            **base,
            "recent_spot_prices": [Decimal("99.99"), Decimal("99.98"), Decimal("99.97")],
        }
    ) is None
    assert choose_maker_momentum_signal(
        **{
            **base,
            "up_quote": OrderBookQuote(bid=Decimal("0.79"), ask=Decimal("0.80")),
            "down_quote": OrderBookQuote(bid=Decimal("0.20"), ask=Decimal("0.21")),
        }
    ) is None


def test_maker_momentum_v2_rejects_weak_flow_but_accepts_strong_lead() -> None:
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
        "side": "UP",
        "trigger_price": Decimal("0.63"),
        "probability_up": Decimal("0.5875"),
        "up_quote": OrderBookQuote(bid=Decimal("0.64"), ask=Decimal("0.65")),
        "down_quote": OrderBookQuote(bid=Decimal("0.35"), ask=Decimal("0.36")),
        "seconds_to_end": Decimal("230"),
        "start_price": Decimal("100"),
    }

    weak = evaluate_maker_momentum_signal(
        **base,
        recent_spot_prices=[Decimal("100.005"), Decimal("100.006"), Decimal("100.007")],
    )
    strong_lead = evaluate_maker_momentum_signal(
        **base,
        recent_spot_prices=[Decimal("100.01"), Decimal("100.02"), Decimal("100.03")],
    )

    assert weak.signal is None
    assert weak.rejection_reason == "weak_flow"
    assert strong_lead.signal is not None
    assert "maker_momentum_v2" in strong_lead.signal.reason


def test_maker_momentum_evaluation_reports_prefilter_reason() -> None:
    market = make_market(
        "Bitcoin Up or Down?",
        "btc-updown-5m-1",
        "c1",
        ("up", "down"),
        "0.01",
        False,
        Decimal("10"),
    )
    result = evaluate_maker_momentum_signal(
        market=market,
        side="UP",
        trigger_price=Decimal("0.40"),
        probability_up=Decimal("0.40"),
        up_quote=OrderBookQuote(bid=Decimal("0.39"), ask=Decimal("0.40")),
        down_quote=OrderBookQuote(bid=Decimal("0.60"), ask=Decimal("0.61")),
        seconds_to_end=Decimal("180"),
        recent_spot_prices=[Decimal("100.01"), Decimal("100.02"), Decimal("100.03")],
        start_price=Decimal("100"),
    )

    assert result.signal is None
    assert result.rejection_reason == "not_favorite"


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


def test_late_favorite_is_limited_to_one_trade_per_window() -> None:
    assert strategy_trade_limit("late_favorite", 5) == 1
    assert strategy_trade_limit("maker_momentum", 5) == 1
    assert strategy_trade_limit("fair_value_edge", 5) == 5


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
