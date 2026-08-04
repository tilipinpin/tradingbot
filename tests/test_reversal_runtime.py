from dataclasses import replace
from decimal import Decimal
from datetime import date

import pytest

from src.polymarket import Market, OrderBookLevel, OrderBookSnapshot
from src.reversal_runtime import (
    GammaResultMismatch,
    ReversalRuntime,
    ReversalRuntimeError,
    dynamic_recovery_decision,
    full_loss_recovery_size,
    _affordable_marketable_buy_size,
    _marketable_buy_size,
    market_health_from_books,
    previous_5m_slug,
    reversal_startup_self_check,
)
from src.reversal_v11 import (
    ActiveRound,
    Direction,
    MarketHealth,
    ReversalSettings,
    ReversalV11,
)


MARKET = Market(
    question="BTC Up or Down",
    slug="btc-updown-5m-700",
    condition_id="0x" + "22" * 32,
    token_ids=("101", "202"),
    minimum_tick_size="0.01",
    neg_risk=False,
    liquidity=Decimal("100"),
    outcomes=("Up", "Down"),
    event_start_time=None,
    end_time=None,
)


def book(token_id: str, bid: str, ask: str, depth: str = "20") -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        timestamp="1",
        bids=(OrderBookLevel(Decimal(bid), Decimal(depth)),),
        asks=(OrderBookLevel(Decimal(ask), Decimal(depth)),),
        minimum_order_size=Decimal("1"),
    )


UP_BOOK = book("101", "0.56", "0.58")
DOWN_BOOK = book("202", "0.42", "0.44")
HEALTHY = MarketHealth(
    short_volatility=Decimal("0.001"),
    absolute_window_move=Decimal("0.002"),
    trend_bid_depth=Decimal("20"),
    trend_spread=Decimal("0.02"),
    estimated_sellable=True,
)


class Receipt:
    transaction_hash = "0xsplit"


class FakeSplitter:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.merge_calls = 0
        self.amounts = []
        self.merge_amounts = []
        self.error = error

    def split(self, **kwargs):
        self.calls += 1
        self.amounts.append(kwargs["amount"])
        if self.error is not None:
            raise self.error
        return Receipt()

    def merge(self, **kwargs):
        self.merge_calls += 1
        self.merge_amounts.append(kwargs["amount"])
        return Receipt()


class FakeTrader:
    def __init__(self) -> None:
        self.balance = Decimal("2")
        self.collateral = Decimal("1000")
        self.orders = []

    def conditional_balance(self, token_id, signature_type):
        assert token_id == "101"
        assert signature_type == 3
        return self.balance

    def collateral_balance(self, signature_type):
        assert signature_type == 3
        return self.collateral

    def sell_limit(self, token_id, price, size, tick_size, neg_risk, order_type="GTC", **kwargs):
        self.orders.append((token_id, price, size, order_type))
        self.balance -= size
        return {
            "success": True,
            "status": "matched",
            "orderID": "exit-order",
            "makingAmount": str(size),
            "takingAmount": str(size * Decimal("0.56")),
        }

    def buy_limit(self, token_id, price, size, tick_size, neg_risk, order_type="GTC", **kwargs):
        self.orders.append((token_id, price, size, order_type))
        self.balance = size
        return {
            "success": True,
            "status": "matched",
            "orderID": "entry-order",
            "makingAmount": str(size * price),
            "takingAmount": str(size),
        }


class ErrorTrader(FakeTrader):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def sell_limit(self, token_id, price, size, tick_size, neg_risk, order_type="GTC", **kwargs):
        self.orders.append((token_id, price, size, order_type))
        raise self.error


class BuyErrorTrader(FakeTrader):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def buy_limit(self, token_id, price, size, tick_size, neg_risk, order_type="GTC", **kwargs):
        self.orders.append((token_id, price, size, order_type))
        raise self.error


class FailOnceBuyTrader(FakeTrader):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def buy_limit(self, token_id, price, size, tick_size, neg_risk, order_type="GTC", **kwargs):
        if not self.failed:
            self.failed = True
            self.orders.append((token_id, price, size, order_type))
            raise RuntimeError(
                "no orders found to match with FAK order. FAK orders are "
                "partially filled or killed if no match is found."
            )
        return super().buy_limit(
            token_id, price, size, tick_size, neg_risk, order_type, **kwargs
        )


def winners(slug: str) -> str | None:
    return {
        "btc-updown-5m-100": "UP",
        "btc-updown-5m-400": "UP",
    }.get(slug)


def seed_two_chainlink_up(runtime: ReversalRuntime) -> None:
    observed = runtime.observe_chainlink_open_prices(
        {
            "btc-updown-5m-100": Decimal("100"),
            "btc-updown-5m-400": Decimal("101"),
            "btc-updown-5m-700": Decimal("102"),
        }
    )
    assert observed == [
        ("btc-updown-5m-100", "observed"),
        ("btc-updown-5m-400", "observed"),
    ]


def test_previous_slug_is_exactly_one_five_minute_window() -> None:
    assert previous_5m_slug(MARKET.slug) == "btc-updown-5m-400"
    assert previous_5m_slug(MARKET.slug, 2) == "btc-updown-5m-100"


@pytest.mark.parametrize(
    ("price", "expected_size", "expected_notional"),
    [
        ("0.50", "2.02", "1.01"),
        ("0.49", "3.00", "1.47"),
        ("0.46", "2.50", "1.15"),
        ("0.44", "2.50", "1.10"),
        ("0.37", "3.00", "1.11"),
    ],
)
def test_marketable_buy_size_satisfies_exchange_precision(
    price: str,
    expected_size: str,
    expected_notional: str,
) -> None:
    size = _marketable_buy_size(
        nominal_shares=Decimal("2"),
        price=Decimal(price),
    )

    assert size == Decimal(expected_size)
    assert size * Decimal(price) == Decimal(expected_notional)


def test_affordable_marketable_buy_size_uses_maximum_exchange_valid_cash() -> None:
    size = _affordable_marketable_buy_size(
        available_collateral=Decimal("2.009999"),
        price=Decimal("0.44"),
    )

    assert size == Decimal("4.50")
    assert size * Decimal("0.44") == Decimal("1.98")


def test_affordable_marketable_buy_size_rejects_below_exchange_minimum() -> None:
    assert _affordable_marketable_buy_size(
        available_collateral=Decimal("1.009999"),
        price=Decimal("0.50"),
    ) == Decimal("0")


def test_dynamic_recovery_sizes_from_loss_and_respects_risk_cap() -> None:
    decision = dynamic_recovery_decision(
        cumulative_loss=Decimal("4"),
        entry_price=Decimal("0.55"),
        retained_side=Direction.DOWN,
        retained_probability=Decimal("0.70"),
        spot_price=Decimal("97"),
        open_price=Decimal("100"),
        settings=replace(
            ReversalSettings(),
            maximum_streak_loss=Decimal("45"),
        ),
    )

    assert decision.allowed
    assert Decimal("0") < decision.shares <= Decimal("16")
    assert decision.projected_streak_loss <= Decimal("45")


def test_dynamic_recovery_skips_when_recovery_would_break_loss_cap() -> None:
    decision = dynamic_recovery_decision(
        cumulative_loss=Decimal("44"),
        entry_price=Decimal("0.55"),
        retained_side=Direction.DOWN,
        retained_probability=Decimal("0.70"),
        spot_price=Decimal("97"),
        open_price=Decimal("100"),
        settings=replace(
            ReversalSettings(),
            maximum_streak_loss=Decimal("45"),
        ),
    )

    assert not decision.allowed
    assert "streak loss limit" in decision.reason


def test_dynamic_recovery_requires_open_price_cross() -> None:
    decision = dynamic_recovery_decision(
        cumulative_loss=Decimal("4"),
        entry_price=Decimal("0.55"),
        retained_side=Direction.DOWN,
        retained_probability=Decimal("0.70"),
        spot_price=Decimal("101"),
        open_price=Decimal("100"),
        settings=ReversalSettings(),
    )

    assert not decision.allowed
    assert "below" in decision.reason


def test_dynamic_recovery_rejects_equal_open_price() -> None:
    decision = dynamic_recovery_decision(
        cumulative_loss=Decimal("4"),
        entry_price=Decimal("0.54"),
        retained_side=Direction.DOWN,
        retained_probability=Decimal("0.70"),
        spot_price=Decimal("100"),
        open_price=Decimal("100"),
        settings=ReversalSettings(),
    )

    assert not decision.allowed
    assert "2 USD buffer" in decision.reason


def test_dynamic_recovery_uses_live_probability_instead_of_fixed_assumption() -> None:
    decision = dynamic_recovery_decision(
        cumulative_loss=Decimal("8"),
        entry_price=Decimal("0.54"),
        retained_side=Direction.DOWN,
        retained_probability=Decimal("0.50"),
        spot_price=Decimal("97"),
        open_price=Decimal("100"),
        settings=ReversalSettings(),
    )

    assert not decision.allowed
    assert decision.expected_value_per_share < 0
    assert "below threshold" in decision.reason


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [
        (1, False),
        (2, False),
        (3, False),
        (4, False),
        (5, False),
        (6, False),
        (7, False),
        (8, False),
        (9, False),
    ],
)
def test_dynamic_recovery_is_disabled_for_all_attempts(
    attempt: int,
    expected: bool,
) -> None:
    assert ReversalSettings().uses_dynamic_recovery(attempt) is expected


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, False), (2, True), (4, True), (5, True), (9, True), (15, True), (16, False)],
)
def test_full_loss_recovery_applies_from_stage_two(
    attempt: int,
    expected: bool,
) -> None:
    assert ReversalSettings().uses_full_loss_recovery(attempt) is expected


def test_full_loss_recovery_covers_all_prior_loss_after_fee() -> None:
    loss = Decimal("15")
    price = Decimal("0.50")

    shares = full_loss_recovery_size(
        cumulative_loss=loss,
        entry_price=price,
    )
    fee = shares * Decimal("0.07") * price * (Decimal("1") - price)

    assert shares * (Decimal("1") - price) - fee >= loss


def test_stage_two_to_four_keep_doubling_floor_when_recovery_needs_less() -> None:
    shares = full_loss_recovery_size(
        cumulative_loss=Decimal("0.50"),
        entry_price=Decimal("0.50"),
        minimum_shares=Decimal("4"),
    )

    assert shares == Decimal("4")


def test_stage_two_to_four_exceed_doubling_floor_when_recovery_needs_more() -> None:
    shares = full_loss_recovery_size(
        cumulative_loss=Decimal("5"),
        entry_price=Decimal("0.50"),
        minimum_shares=Decimal("4"),
    )

    assert shares > Decimal("4")
    fee = shares * Decimal("0.07") * Decimal("0.50") * Decimal("0.50")
    assert shares * Decimal("0.50") - fee >= Decimal("5")


def test_stage_five_to_fifteen_targets_all_prior_loss() -> None:
    loss = Decimal("15")
    price = Decimal("0.50")

    shares = full_loss_recovery_size(
        cumulative_loss=loss,
        entry_price=price,
        recovery_fraction=Decimal("1.00"),
    )
    fee = shares * Decimal("0.07") * price * (Decimal("1") - price)
    recovered = shares * (Decimal("1") - price) - fee

    assert recovered >= loss


def test_full_loss_recovery_reprices_remaining_partial_fill() -> None:
    shares = full_loss_recovery_size(
        cumulative_loss=Decimal("15"),
        entry_price=Decimal("0.55"),
        filled_shares=Decimal("10"),
        filled_cost=Decimal("5"),
        filled_fees=Decimal("0.175"),
    )

    assert shares > Decimal("10")


def test_completed_prices_settle_immediately_and_queue_gamma_verification(tmp_path) -> None:
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=None,
        signature_type=3,
        live=False,
    )

    observed = runtime.observe_completed_window_prices(
        {
            "btc-updown-5m-100": (Decimal("100"), Decimal("101")),
            "btc-updown-5m-400": (Decimal("101"), Decimal("99")),
        }
    )

    assert observed == [
        ("btc-updown-5m-100", "observed"),
        ("btc-updown-5m-400", "observed"),
    ]
    assert runtime.strategy.state.recent_results == [Direction.UP, Direction.DOWN]
    assert runtime.strategy.state.pending_gamma_results == {
        "btc-updown-5m-100": Direction.UP,
        "btc-updown-5m-400": Direction.DOWN,
    }


def test_live_runtime_bootstraps_two_results_splits_once_and_exits_trend(tmp_path) -> None:
    splitter = FakeSplitter()
    trader = FakeTrader()
    orders = []
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=splitter,
        trader=trader,
        signature_type=3,
        live=True,
        order_callback=orders.append,
    )
    seed_two_chainlink_up(runtime)

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "exit_complete"
    assert result.plan is not None and result.plan.retained_side is Direction.DOWN
    assert splitter.calls == 1
    assert trader.orders == [("101", Decimal("0.56"), Decimal("2"), "FAK")]
    assert len(orders) == 1
    active = runtime.strategy.state.active_round
    assert active is not None
    assert active.execution_phase == "trend_exit_complete"
    assert active.exit_sold_shares == Decimal("2")


def test_direct_buy_runtime_skips_split_and_buys_reversal_side(tmp_path) -> None:
    splitter = FakeSplitter()
    trader = FakeTrader()
    trader.balance = Decimal("0")
    orders = []
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=splitter,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
        order_callback=orders.append,
    )
    seed_two_chainlink_up(runtime)

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "entry_complete"
    assert splitter.calls == 0
    assert trader.orders == [("202", Decimal("0.44"), Decimal("2.50"), "FAK")]
    assert orders[0]["order_role"] == "reversal_direct_entry"
    active = runtime.strategy.state.active_round
    assert active is not None
    assert active.execution_phase == "direct_entry_complete"
    assert active.exit_sold_shares == Decimal("2.50")
    assert active.exit_sell_proceeds == Decimal("2.50") * Decimal("0.44")


def test_direct_buy_amount_precision_rejection_is_safely_retryable(tmp_path) -> None:
    trader = BuyErrorTrader(
        RuntimeError(
            "invalid amounts, the market buy orders maker amount supports a max "
            "accuracy of 2 decimals, taker amount a max of 4 decimals"
        )
    )
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )
    seed_two_chainlink_up(runtime)

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "entry_amount_rejected"
    assert runtime.strategy.state.active_round is not None
    assert runtime.strategy.state.active_round.execution_phase == "direct_entry_ready"
    assert runtime.strategy.metrics().unmatched_orders == 1
    assert runtime.strategy.metrics().api_order_errors == 1


def test_direct_buy_second_attempt_does_not_use_fair_value_edge_filter(tmp_path) -> None:
    trader = FakeTrader()
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )
    seed_two_chainlink_up(runtime)
    first = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
        probability_up=Decimal("0.50"),
    )
    assert first.status == "entry_complete"
    assert runtime.strategy.settle_window(MARKET.slug, Direction.UP) == "trend_continued"

    next_market = replace(MARKET, slug="btc-updown-5m-1000")
    result = runtime.tick(
        market=next_market,
        up_book=UP_BOOK,
        down_book=book("202", "0.52", "0.54"),
        health=HEALTHY,
        probability_up=Decimal("0.50"),
    )

    assert result.status == "entry_complete"
    assert len(trader.orders) == 2
    assert runtime.strategy.state.active_round is not None
    assert runtime.strategy.state.active_round.failures == 1
    assert runtime.strategy.state.active_round.execution_phase == "direct_entry_complete"


def test_recovery_entry_retries_after_first_fak_has_no_match(tmp_path) -> None:
    strategy = ReversalV11()
    strategy.state.active_round = ActiveRound(
        round_id=203,
        trend_side=Direction.DOWN,
        failures=3,
        awaiting_window=MARKET.slug,
        committed=Decimal("30"),
        cumulative_loss=Decimal("7.744342"),
        execution_phase="planned",
        planned_shares=Decimal("16"),
    )
    trader = FailOnceBuyTrader()
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )
    seed_two_chainlink_up(runtime)

    first = runtime.tick(
        market=MARKET,
        up_book=book("101", "0.60", "0.61"),
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )
    assert first.status == "entry_unmatched"
    assert strategy.state.last_opening_processed_slug == MARKET.slug
    assert strategy.state.active_round is not None
    assert strategy.state.active_round.execution_phase == "direct_entry_ready"

    second = runtime.tick(
        market=MARKET,
        up_book=book("101", "0.56", "0.57"),
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert second.status == "entry_complete"
    assert len(trader.orders) == 2
    assert trader.orders[0][1] == Decimal("0.61")
    assert trader.orders[1][1] == Decimal("0.57")
    assert strategy.state.active_round is not None
    assert strategy.state.active_round.execution_phase == "direct_entry_complete"


def test_late_recovery_stops_round_when_profit_target_is_unfunded(tmp_path) -> None:
    strategy = ReversalV11(ReversalSettings(minimum_round_profit=Decimal("2")))
    strategy.state.active_round = ActiveRound(
        round_id=1,
        trend_side=Direction.UP,
        failures=4,
        awaiting_window=MARKET.slug,
        committed=Decimal("16"),
        cumulative_loss=Decimal("100"),
        execution_phase="planned",
        planned_shares=Decimal("16"),
    )
    trader = FakeTrader()
    trader.balance = Decimal("0")
    trader.collateral = Decimal("2.009999")
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )
    seed_two_chainlink_up(runtime)

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "profit_target_unfunded"
    assert trader.orders == []
    assert result.plan is not None
    assert result.plan.attempt == 5
    assert strategy.state.active_round is None


def test_first_stage_sizes_for_two_pusd_minimum_net_profit(tmp_path) -> None:
    trader = FakeTrader()
    trader.balance = Decimal("0")
    runtime = ReversalRuntime(
        strategy=ReversalV11(
            ReversalSettings(minimum_round_profit=Decimal("2"))
        ),
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )
    seed_two_chainlink_up(runtime)

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "entry_complete"
    shares = trader.orders[0][2]
    price = trader.orders[0][1]
    fee = shares * Decimal("0.07") * price * (Decimal("1") - price)
    assert shares * (Decimal("1") - price) - fee >= Decimal("2")


def test_later_stage_covers_prior_loss_plus_two_pusd_profit() -> None:
    loss = Decimal("15")
    price = Decimal("0.50")

    shares = full_loss_recovery_size(
        cumulative_loss=loss,
        entry_price=price,
        minimum_profit=Decimal("2"),
    )
    fee = shares * Decimal("0.07") * price * (Decimal("1") - price)

    assert shares * (Decimal("1") - price) - fee >= loss + Decimal("2")


def test_live_runtime_refreshes_book_after_split_and_sells_at_latest_bid(tmp_path) -> None:
    splitter = FakeSplitter()
    trader = FakeTrader()
    refreshed_up = book("101", "0.61", "0.63")
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=splitter,
        trader=trader,
        signature_type=3,
        live=True,
    )
    seed_two_chainlink_up(runtime)

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
        book_refresh=lambda: (refreshed_up, DOWN_BOOK),
    )

    assert result.status == "exit_complete"
    assert trader.orders == [("101", Decimal("0.61"), Decimal("2"), "FAK")]


def completed_first_attempt() -> ReversalV11:
    strategy = ReversalV11()
    strategy.settle_window("btc-updown-5m-100", "UP")
    strategy.settle_window("btc-updown-5m-400", "UP")
    prior = strategy.plan_window(MARKET.slug, HEALTHY)
    assert prior is not None
    strategy.mark_split_submitting(prior)
    strategy.mark_split_confirmed(prior, "0xprior")
    strategy.record_exit_fill(prior, shares=Decimal("2"), proceeds=Decimal("1"))
    return strategy


def test_active_round_presplits_next_stage_before_previous_result(tmp_path) -> None:
    splitter = FakeSplitter()
    strategy = completed_first_attempt()
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=splitter,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )
    next_market = replace(MARKET, slug="btc-updown-5m-1000")

    result = runtime.prepare_active_round_opening_split(next_market)

    assert result is not None and result.status == "opening_split_prepared"
    assert splitter.calls == 1
    assert splitter.amounts == [Decimal("4")]
    assert strategy.state.prepared_split is not None
    assert strategy.state.prepared_split.execution_phase == "split_confirmed"
    assert strategy.state.active_round is not None
    assert strategy.state.active_round.awaiting_window == MARKET.slug


def test_presplit_is_adopted_without_second_split_when_trend_continues(tmp_path) -> None:
    splitter = FakeSplitter()
    strategy = completed_first_attempt()
    trader = FakeTrader()
    trader.balance = Decimal("4")
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=splitter,
        trader=trader,
        signature_type=3,
        live=True,
    )
    next_market = replace(MARKET, slug="btc-updown-5m-1000")
    runtime.prepare_active_round_opening_split(next_market)
    strategy.settle_window(MARKET.slug, "UP")

    result = runtime.tick(
        market=next_market,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "exit_complete"
    assert splitter.calls == 1
    assert trader.orders == [("101", Decimal("0.56"), Decimal("4"), "FAK")]


def test_presplit_is_merged_when_previous_window_reverses(tmp_path) -> None:
    splitter = FakeSplitter()
    strategy = completed_first_attempt()
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=splitter,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )
    next_market = replace(MARKET, slug="btc-updown-5m-1000")
    runtime.prepare_active_round_opening_split(next_market)
    strategy.settle_window(MARKET.slug, "DOWN")

    result = runtime.tick(
        market=next_market,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "unused_split_merged"
    assert splitter.calls == 1
    assert splitter.merge_calls == 1
    assert strategy.state.prepared_split is None


def test_fifteenth_attempt_never_presplits_a_sixteenth_stage(tmp_path) -> None:
    strategy = ReversalV11()
    strategy.state.active_round = ActiveRound(
        round_id=1,
        trend_side=Direction.UP,
        failures=14,
        awaiting_window=MARKET.slug,
        execution_phase="trend_exit_complete",
    )
    splitter = FakeSplitter()
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=splitter,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )

    result = runtime.prepare_active_round_opening_split(
        replace(MARKET, slug="btc-updown-5m-1000")
    )

    assert result is None
    assert splitter.calls == 0


def test_live_runtime_treats_fak_no_match_as_retryable_unmatched_exit(tmp_path) -> None:
    trader = ErrorTrader(
        RuntimeError(
            "no orders found to match with FAK order. FAK orders are partially "
            "filled or killed if no match is found."
        )
    )
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=FakeSplitter(),
        trader=trader,
        signature_type=3,
        live=True,
    )
    seed_two_chainlink_up(runtime)

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "exit_unmatched"
    assert runtime.strategy.state.active_round is not None
    assert runtime.strategy.state.active_round.execution_phase == "split_confirmed"
    assert runtime.strategy.metrics().unmatched_orders == 1


def test_live_runtime_still_raises_non_matching_order_errors(tmp_path) -> None:
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=FakeSplitter(),
        trader=ErrorTrader(RuntimeError("invalid signature")),
        signature_type=3,
        live=True,
    )
    seed_two_chainlink_up(runtime)

    with pytest.raises(RuntimeError, match="invalid signature"):
        runtime.tick(
            market=MARKET,
            up_book=UP_BOOK,
            down_book=DOWN_BOOK,
            health=HEALTHY,
        )


def test_restart_after_confirmed_split_resumes_exit_without_second_split(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    strategy = ReversalV11()
    strategy.settle_window("btc-updown-5m-100", "UP")
    strategy.settle_window("btc-updown-5m-400", "UP")
    plan = strategy.plan_window(MARKET.slug, HEALTHY)
    assert plan is not None
    strategy.mark_split_submitting(plan)
    strategy.mark_split_confirmed(plan, "0xalready-split")
    strategy.dump(state_path)
    splitter = FakeSplitter()
    runtime = ReversalRuntime(
        strategy=ReversalV11.load(state_path),
        state_path=state_path,
        winner_lookup=winners,
        splitter=splitter,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "exit_complete"
    assert splitter.calls == 0


def test_ambiguous_split_failure_is_persisted_and_never_retried(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    splitter = FakeSplitter(TimeoutError("unknown submission outcome"))
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=state_path,
        winner_lookup=winners,
        splitter=splitter,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )
    seed_two_chainlink_up(runtime)

    with pytest.raises(TimeoutError):
        runtime.tick(
            market=MARKET,
            up_book=UP_BOOK,
            down_book=DOWN_BOOK,
            health=HEALTHY,
        )
    restored = ReversalV11.load(state_path)
    assert restored.state.active_round is not None
    assert restored.state.active_round.execution_phase == "planned"
    assert restored.state.prepared_split.execution_phase == "split_uncertain"

    with pytest.raises(ReversalRuntimeError, match="automatic retry is blocked"):
        runtime.tick(
            market=MARKET,
            up_book=UP_BOOK,
            down_book=DOWN_BOOK,
            health=HEALTHY,
        )
    assert splitter.calls == 1


def test_runtime_waits_for_chainlink_boundary_and_skips_when_streak_is_absent(tmp_path) -> None:
    outcomes = {
        "btc-updown-5m-100": "UP",
        "btc-updown-5m-400": None,
    }
    splitter = FakeSplitter()
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=lambda slug: outcomes.get(slug),
        splitter=splitter,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )

    waiting = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )
    assert waiting.status == "waiting_chainlink_boundary_no_split"
    assert splitter.calls == 0
    assert splitter.merge_calls == 0

    outcomes["btc-updown-5m-400"] = "DOWN"
    runtime.observe_chainlink_open_prices(
        {
            "btc-updown-5m-100": Decimal("100"),
            "btc-updown-5m-400": Decimal("101"),
            "btc-updown-5m-700": Decimal("100"),
        }
    )
    skipped = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )
    assert skipped.status == "no_trigger_no_split"
    assert splitter.calls == 0
    assert splitter.merge_calls == 0
    assert runtime.strategy.state.prepared_split is None

    repeated = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )
    assert repeated.status == "opening_already_processed"
    assert splitter.calls == 0
    assert splitter.merge_calls == 0


def test_runtime_three_streak_requires_all_three_immediate_results(tmp_path) -> None:
    market = replace(MARKET, slug="btc-updown-5m-1000")
    splitter = FakeSplitter()
    runtime = ReversalRuntime(
        strategy=ReversalV11(ReversalSettings(trigger_streak=3)),
        state_path=tmp_path / "state.json",
        winner_lookup=lambda slug: None,
        splitter=splitter,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )
    runtime.observe_chainlink_open_prices(
        {
            "btc-updown-5m-100": Decimal("100"),
            "btc-updown-5m-400": Decimal("101"),
            "btc-updown-5m-700": Decimal("102"),
            "btc-updown-5m-1000": Decimal("103"),
        }
    )

    result = runtime.tick(
        market=market,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "exit_complete"
    assert result.plan is not None
    assert result.plan.attempt == 1
    assert result.plan.trend_side is Direction.UP
    assert result.plan.retained_side is Direction.DOWN
    assert splitter.calls == 1


def test_chainlink_results_trigger_before_gamma_is_final(tmp_path) -> None:
    splitter = FakeSplitter()
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=lambda slug: None,
        splitter=splitter,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )
    seed_two_chainlink_up(runtime)

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "exit_complete"
    assert splitter.calls == 1
    assert runtime.strategy.state.pending_gamma_results == {
        "btc-updown-5m-100": Direction.UP,
        "btc-updown-5m-400": Direction.UP,
    }


def test_tick_never_waits_for_gamma_review_before_split_and_exit(tmp_path) -> None:
    gamma_calls: list[str] = []

    def unavailable_gamma(slug: str) -> str | None:
        gamma_calls.append(slug)
        raise AssertionError("Gamma must not run in the transaction path")

    splitter = FakeSplitter()
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=unavailable_gamma,
        splitter=splitter,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )
    seed_two_chainlink_up(runtime)

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "exit_complete"
    assert splitter.calls == 1
    assert gamma_calls == []


def test_gamma_mismatch_raises_and_remains_pending_for_manual_review(tmp_path) -> None:
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=lambda slug: "DOWN" if slug.endswith("100") else None,
        splitter=FakeSplitter(),
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )
    seed_two_chainlink_up(runtime)

    with pytest.raises(
        GammaResultMismatch, match="Chainlink=UP Gamma=DOWN"
    ) as raised:
        runtime.verify_gamma_results()

    assert raised.value.slug == "btc-updown-5m-100"
    assert raised.value.provisional is Direction.UP
    assert raised.value.official is Direction.DOWN
    assert runtime.strategy.state.pending_gamma_results[
        "btc-updown-5m-100"
    ] is Direction.UP

    runtime.quarantine_gamma_mismatch(raised.value)
    restored = ReversalV11.load(tmp_path / "state.json")

    assert "btc-updown-5m-100" not in restored.state.pending_gamma_results
    assert restored.state.gamma_mismatch_slugs == ["btc-updown-5m-100"]
    assert restored.metrics().api_order_errors == 1


def test_confirmed_legacy_unused_split_is_merged_for_recovery(tmp_path) -> None:
    strategy = ReversalV11()
    prepared = strategy.prepare_opening_split(MARKET.slug)
    strategy.mark_opening_split_submitting()
    strategy.mark_opening_split_confirmed("0xlegacy")
    assert prepared.amount == Decimal("2")
    splitter = FakeSplitter()
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=lambda slug: {
            "btc-updown-5m-100": "UP",
            "btc-updown-5m-400": "DOWN",
        }.get(slug),
        splitter=splitter,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )
    runtime.observe_chainlink_open_prices(
        {
            "btc-updown-5m-100": Decimal("100"),
            "btc-updown-5m-400": Decimal("101"),
            "btc-updown-5m-700": Decimal("100"),
        }
    )

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "unused_split_merged"
    assert splitter.calls == 0
    assert splitter.merge_calls == 1


def test_ambiguous_exit_is_reconciled_from_zero_token_balance_without_resell(tmp_path) -> None:
    class AmbiguousExitTrader(FakeTrader):
        def sell_limit(self, token_id, price, size, tick_size, neg_risk, order_type="GTC", **kwargs):
            self.orders.append((token_id, price, size, order_type))
            self.balance = Decimal("0")
            raise TimeoutError("sell response lost after submission")

    trader = AmbiguousExitTrader()
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=FakeSplitter(),
        trader=trader,
        signature_type=3,
        live=True,
    )
    seed_two_chainlink_up(runtime)

    with pytest.raises(TimeoutError):
        runtime.tick(
            market=MARKET,
            up_book=UP_BOOK,
            down_book=DOWN_BOOK,
            health=HEALTHY,
        )
    assert runtime.strategy.state.active_round.execution_phase == "trend_exit_submitting"

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "exit_reconciled"
    assert len(trader.orders) == 1


def test_health_uses_trend_side_spread_depth_and_price_move() -> None:
    health = market_health_from_books(
        trend_side=Direction.UP,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        making_amount=Decimal("16"),
        spot_prices=[Decimal("100"), Decimal("100.1")],
        open_price=Decimal("100"),
    )

    assert health.trend_spread == Decimal("0.02")
    assert health.trend_bid_depth == Decimal("20")
    assert health.estimated_sellable is True
    assert health.absolute_window_move == Decimal("0.001")


def test_startup_self_check_is_read_only_and_requires_no_open_orders() -> None:
    class Preflight:
        wallet = "0x1111111111111111111111111111111111111111"
        collateral_balance_units = 50_000_000

    class Submitter:
        prewarmed = False

        def read_only_self_check(self):
            return {"deployed": True}

        def prewarm(self):
            self.prewarmed = True

    class SplitterForCheck:
        submitter = Submitter()

        def preflight(self, **kwargs):
            assert kwargs["amount"] == Decimal("30")
            return Preflight()

    class TraderForCheck:
        def open_orders(self):
            return []

        def conditional_balance(self, token_id, signature_type):
            assert signature_type == 3
            return Decimal("0")

    report = reversal_startup_self_check(
        market=MARKET,
        splitter=SplitterForCheck(),
        trader=TraderForCheck(),
        signature_type=3,
    )

    assert report.collateral_units == 50_000_000
    assert report.open_orders == 0
    assert report.relayer_deployed is True
    assert SplitterForCheck.submitter.prewarmed is True


def test_startup_self_check_blocks_existing_orders() -> None:
    class Preflight:
        wallet = "0x1111111111111111111111111111111111111111"
        collateral_balance_units = 50_000_000

    class SplitterForCheck:
        submitter = object()

        def preflight(self, **kwargs):
            return Preflight()

    class TraderForCheck:
        def open_orders(self):
            return [{"id": "existing"}]

        def conditional_balance(self, token_id, signature_type):
            return Decimal("0")

    with pytest.raises(ReversalRuntimeError, match="existing CLOB order"):
        reversal_startup_self_check(
            market=MARKET,
            splitter=SplitterForCheck(),
            trader=TraderForCheck(),
            signature_type=3,
        )


def test_direct_buy_startup_skips_relayer_and_checks_clob_collateral() -> None:
    class TraderForCheck:
        def open_orders(self):
            return []

        def conditional_balance(self, token_id, signature_type):
            return Decimal("0")

        def collateral_balance(self, signature_type):
            assert signature_type == 3
            return Decimal("31.25")

    report = reversal_startup_self_check(
        market=MARKET,
        splitter=None,
        trader=TraderForCheck(),
        signature_type=3,
        execution_mode="direct_buy",
        wallet="0xfunder",
    )

    assert report.wallet == "0xfunder"
    assert report.collateral_units == 31_250_000
    assert report.relayer_deployed is False


def test_daily_report_is_sent_and_persisted_only_once(tmp_path) -> None:
    sent = []
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=lambda slug: None,
        splitter=None,
        trader=None,
        signature_type=3,
        live=False,
    )

    assert runtime.send_daily_report_once(date(2026, 7, 27), lambda msg: sent.append(msg) or True)
    assert not runtime.send_daily_report_once(date(2026, 7, 27), lambda msg: sent.append(msg) or True)
    assert len(sent) == 1
    assert "2026-07-27" in sent[0]
    restored = ReversalV11.load(tmp_path / "state.json")
    assert restored.state.reported_days == ["2026-07-27"]
