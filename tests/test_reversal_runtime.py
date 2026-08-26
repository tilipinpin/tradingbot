from dataclasses import replace
from decimal import Decimal
from datetime import date
from threading import Event
import time

import pytest

from src.polymarket import Market, OrderBookLevel, OrderBookSnapshot
from src.reversal_runtime import (
    ChainResultMismatch,
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
    COMPACT_REVERSAL_STAKES,
    FIRST_STAGE_ONLY_STAKES,
    SPARSE_RECOVERY_NOTIONALS,
    TWO_WINDOW_FIXED_NOTIONALS,
    Direction,
    MarketHealth,
    ReversalSettings,
    ReversalV11,
    TradePlan,
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
    [
        (1, False),
        (2, False),
        (4, False),
        (5, False),
        (6, True),
        (15, True),
        (16, True),
        (25, True),
        (26, False),
    ],
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


def test_recovery_size_can_keep_a_minimum_floor() -> None:
    shares = full_loss_recovery_size(
        cumulative_loss=Decimal("0.50"),
        entry_price=Decimal("0.50"),
        minimum_shares=Decimal("4"),
    )

    assert shares == Decimal("4")


def test_recovery_size_can_exceed_a_minimum_floor() -> None:
    shares = full_loss_recovery_size(
        cumulative_loss=Decimal("5"),
        entry_price=Decimal("0.50"),
        minimum_shares=Decimal("4"),
    )

    assert shares > Decimal("4")
    fee = shares * Decimal("0.07") * Decimal("0.50") * Decimal("0.50")
    assert shares * Decimal("0.50") - fee >= Decimal("5")


def test_stage_six_to_twenty_five_targets_all_prior_loss() -> None:
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


def test_stage_two_keeps_fixed_floor_and_sizes_to_round_break_even(tmp_path) -> None:
    strategy = ReversalV11(
        ReversalSettings(
            full_loss_recovery_start_attempt=2,
            full_loss_recovery_strict_funding=True,
        )
    )
    strategy.state.active_round = ActiveRound(
        round_id=1,
        trend_side=Direction.UP,
        failures=1,
        awaiting_window=MARKET.slug,
        committed=Decimal("2"),
        cumulative_loss=Decimal("3"),
        execution_phase="planned",
        planned_shares=Decimal("2"),
    )
    trader = FakeTrader()
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

    assert result.status == "entry_complete"
    _, price, shares, _ = trader.orders[0]
    fee = shares * Decimal("0.07") * price * (Decimal("1") - price)
    assert shares >= Decimal("2")
    assert shares * (Decimal("1") - price) - fee >= Decimal("3")


def test_stage_two_break_even_order_is_not_reduced_when_balance_is_short(tmp_path) -> None:
    strategy = ReversalV11(
        ReversalSettings(
            full_loss_recovery_start_attempt=2,
            full_loss_recovery_strict_funding=True,
        )
    )
    strategy.state.active_round = ActiveRound(
        round_id=1,
        trend_side=Direction.UP,
        failures=1,
        awaiting_window=MARKET.slug,
        committed=Decimal("2"),
        cumulative_loss=Decimal("3"),
        execution_phase="planned",
        planned_shares=Decimal("2"),
    )
    trader = FakeTrader()
    trader.collateral = Decimal("1.01")
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

    assert result.status == "break_even_target_unfunded"
    assert trader.orders == []
    assert strategy.state.active_round is None


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


def test_completed_prices_apply_official_equal_means_up_rule(tmp_path) -> None:
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
        {"btc-updown-5m-100": (Decimal("79122.1678"), Decimal("79122.1678"))}
    )

    assert observed == [("btc-updown-5m-100", "observed")]
    assert runtime.strategy.state.recent_results == [Direction.UP]


def test_terminal_book_result_settles_and_queues_gamma_verification(tmp_path) -> None:
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=None,
        signature_type=3,
        live=False,
    )

    observed = runtime.observe_completed_window_results(
        {"btc-updown-5m-100": Direction.DOWN}
    )

    assert observed == [("btc-updown-5m-100", "observed")]
    assert runtime.strategy.state.recent_results == [Direction.DOWN]
    assert runtime.strategy.state.pending_gamma_results == {
        "btc-updown-5m-100": Direction.DOWN
    }


def test_boundary_mode_switch_clears_prices_but_preserves_active_round(tmp_path) -> None:
    strategy = ReversalV11()
    strategy.state.chainlink_open_prices = {
        "btc-updown-5m-100": Decimal("100"),
        "btc-updown-5m-400": Decimal("101"),
    }
    strategy.state.active_round = ActiveRound(
        round_id=7,
        trend_side=Direction.UP,
        failures=3,
        cumulative_loss=Decimal("9.25"),
    )
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=None,
        signature_type=3,
        live=False,
    )

    assert runtime.set_boundary_price_mode("twap60")
    assert strategy.state.chainlink_price_mode == "twap60"
    assert strategy.state.chainlink_open_prices == {}
    assert strategy.state.active_round is not None
    assert strategy.state.active_round.failures == 3
    assert strategy.state.active_round.cumulative_loss == Decimal("9.25")
    assert not runtime.set_boundary_price_mode("twap60")


def test_twap_ptb_open_mode_migration_clears_old_twap_boundary_cache(tmp_path) -> None:
    strategy = ReversalV11()
    strategy.state.chainlink_price_mode = "twap60"
    strategy.state.chainlink_open_prices = {
        "btc-updown-5m-100": Decimal("100"),
        "btc-updown-5m-400": Decimal("101"),
    }
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=None,
        signature_type=3,
        live=False,
    )

    assert runtime.set_boundary_price_mode("twap60_ptb_open")
    assert strategy.state.chainlink_price_mode == "twap60_ptb_open"
    assert strategy.state.chainlink_open_prices == {}


def test_official_boundary_v2_migration_preserves_active_round(tmp_path) -> None:
    strategy = ReversalV11()
    strategy.state.chainlink_price_mode = "twap60_ptb_open"
    strategy.state.chainlink_open_prices = {
        "btc-updown-5m-100": Decimal("100"),
        "btc-updown-5m-400": Decimal("99"),
    }
    strategy.state.active_round = ActiveRound(
        round_id=9,
        trend_side=Direction.UP,
        failures=4,
        cumulative_loss=Decimal("12.50"),
    )
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=None,
        signature_type=3,
        live=False,
    )

    assert runtime.set_boundary_price_mode("twap_official_boundary_v2")
    assert strategy.state.chainlink_open_prices == {}
    assert strategy.state.active_round is not None
    assert strategy.state.active_round.round_id == 9
    assert strategy.state.active_round.failures == 4
    assert strategy.state.active_round.cumulative_loss == Decimal("12.50")


def test_twap_chainlink_boundary_v3_migration_clears_incompatible_prices(tmp_path) -> None:
    strategy = ReversalV11()
    strategy.state.chainlink_price_mode = "twap_official_boundary_v2"
    strategy.state.chainlink_open_prices = {
        "btc-updown-5m-100": Decimal("100"),
        "btc-updown-5m-400": Decimal("99"),
    }
    strategy.state.active_round = ActiveRound(
        round_id=10,
        trend_side=Direction.DOWN,
        failures=2,
        cumulative_loss=Decimal("3.75"),
    )
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=None,
        signature_type=3,
        live=False,
    )

    assert runtime.set_boundary_price_mode("twap60_chainlink_boundary_v3")
    assert strategy.state.chainlink_open_prices == {}
    assert strategy.state.active_round is not None
    assert strategy.state.active_round.round_id == 10
    assert strategy.state.active_round.failures == 2
    assert strategy.state.active_round.cumulative_loss == Decimal("3.75")


def test_record_chainlink_open_price_does_not_settle_adjacent_windows(tmp_path) -> None:
    strategy = ReversalV11()
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=None,
        signature_type=3,
        live=False,
    )

    runtime.record_chainlink_open_price("btc-updown-5m-100", Decimal("100"))
    runtime.record_chainlink_open_price("btc-updown-5m-400", Decimal("101"))

    assert strategy.state.chainlink_open_prices == {
        "btc-updown-5m-100": Decimal("100"),
        "btc-updown-5m-400": Decimal("101"),
    }
    assert strategy.state.last_settled_slug is None
    assert strategy.state.pending_gamma_results == {}

    with pytest.raises(ReversalRuntimeError, match="open price changed"):
        runtime.record_chainlink_open_price("btc-updown-5m-400", Decimal("102"))
    runtime.record_chainlink_open_price(
        "btc-updown-5m-400",
        Decimal("102"),
        allow_correction=True,
    )
    assert strategy.state.chainlink_open_prices["btc-updown-5m-400"] == Decimal("102")
    assert strategy.state.last_settled_slug is None


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
    assert trader.orders == [("101", Decimal("0.56"), Decimal("1"), "FAK")]
    assert len(orders) == 1
    active = runtime.strategy.state.active_round
    assert active is not None
    assert active.execution_phase == "trend_exit_complete"
    assert active.exit_sold_shares == Decimal("1")


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


def test_compact_second_stage_requires_unlocked_profit_and_price_depth(tmp_path) -> None:
    settings = ReversalSettings(
        trigger_streak=4,
        stakes=COMPACT_REVERSAL_STAKES,
        compact_two_stage_enabled=True,
        full_loss_recovery_enabled=False,
    )
    strategy = ReversalV11(settings)
    strategy.state.active_round = ActiveRound(
        round_id=1,
        trend_side=Direction.UP,
        failures=1,
        awaiting_window=MARKET.slug,
        execution_phase="planned",
        planned_shares=Decimal("4"),
        cumulative_loss=Decimal("1.05"),
    )
    plan = TradePlan(
        round_id=1,
        window_slug=MARKET.slug,
        attempt=2,
        making_amount=Decimal("4"),
        trend_side=Direction.UP,
        retained_side=Direction.DOWN,
    )
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
        unlocked_profit_lookup=lambda fraction: Decimal("0"),
    )

    assert "unlocked profit" in str(
        runtime._compact_entry_block_reason(plan=plan, book=DOWN_BOOK)
    )
    runtime.unlocked_profit_lookup = lambda fraction: Decimal("10")
    assert runtime._compact_entry_block_reason(plan=plan, book=DOWN_BOOK) is None
    assert "exceeds stage-2 limit" in str(
        runtime._compact_entry_block_reason(
            plan=plan,
            book=book("202", "0.44", "0.46"),
        )
    )
    assert "depth" in str(
        runtime._compact_entry_block_reason(
            plan=plan,
            book=book("202", "0.42", "0.44", depth="2"),
        )
    )


def test_first_stage_only_entry_checks_only_price_spread_and_depth(tmp_path) -> None:
    settings = ReversalSettings(
        trigger_streak=4,
        stakes=FIRST_STAGE_ONLY_STAKES,
        first_stage_only_enabled=True,
        full_loss_recovery_enabled=False,
    )
    strategy = ReversalV11(settings)
    strategy.state.active_round = ActiveRound(
        round_id=1,
        trend_side=Direction.UP,
        awaiting_window=MARKET.slug,
        execution_phase="planned",
        planned_shares=Decimal("1"),
    )
    plan = TradePlan(
        round_id=1,
        window_slug=MARKET.slug,
        attempt=1,
        making_amount=Decimal("1"),
        trend_side=Direction.UP,
        retained_side=Direction.DOWN,
    )
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )

    assert runtime._first_stage_entry_block_reason(
        plan=plan, book=book("202", "0.59", "0.64", "20")
    ) is None
    assert "exceeds first-stage limit" in str(
        runtime._first_stage_entry_block_reason(
            plan=plan, book=book("202", "0.62", "0.65", "20")
        )
    )
    assert "spread" in str(
        runtime._first_stage_entry_block_reason(
            plan=plan, book=book("202", "0.58", "0.64", "20")
        )
    )
    assert "depth" in str(
        runtime._first_stage_entry_block_reason(
            plan=plan, book=book("202", "0.59", "0.64", "0.5")
        )
    )


def test_standard_four_window_profile_applies_first_stage_ask_limit(tmp_path) -> None:
    settings = ReversalSettings(
        trigger_streak=4,
        maximum_attempts=10,
        first_attempt_uses_first_stage_rules=True,
    )
    trader = FakeTrader()
    runtime = ReversalRuntime(
        strategy=ReversalV11(settings),
        state_path=tmp_path / "standard-first-stage.json",
        winner_lookup=lambda slug: None,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )
    runtime.observe_chainlink_open_prices(
        {
            f"btc-updown-5m-{epoch}": Decimal(str(index))
            for index, epoch in enumerate((100, 400, 700, 1000, 1300), start=1)
        }
    )

    result = runtime.tick(
        market=replace(MARKET, slug="btc-updown-5m-1300"),
        up_book=UP_BOOK,
        down_book=book("202", "0.62", "0.65", "20"),
        health=HEALTHY,
    )

    assert result.status == "first_stage_entry_filtered"
    assert trader.orders == []


def fixed_notional_runtime(tmp_path, ask: str) -> tuple[ReversalRuntime, FakeTrader, Market]:
    settings = ReversalSettings(
        trigger_streak=2,
        stakes=TWO_WINDOW_FIXED_NOTIONALS,
        fixed_notional_stages=TWO_WINDOW_FIXED_NOTIONALS,
        fixed_notional_max_ask=Decimal("0.49"),
        continue_final_stage_until_success_or_unfunded=True,
        full_loss_recovery_enabled=False,
    )
    strategy = ReversalV11(settings)
    for epoch in (400, 700):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.UP)
    trader = FakeTrader()
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / f"fixed-{ask}.json",
        winner_lookup=winners,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )
    return runtime, trader, replace(MARKET, slug="btc-updown-5m-1000")


def test_fixed_notional_profile_converts_1_pusd_budget_to_shares(tmp_path) -> None:
    runtime, trader, market = fixed_notional_runtime(tmp_path, "0.40")

    result = runtime.tick(
        market=market,
        up_book=UP_BOOK,
        down_book=book("202", "0.39", "0.40", "20"),
        health=HEALTHY,
    )

    assert result.status == "entry_complete"
    assert trader.orders == [("202", Decimal("0.40"), Decimal("2.55"), "FAK")]
    assert trader.orders[0][1] * trader.orders[0][2] == Decimal("1.0200")


def sparse_runtime(tmp_path, *, failures: int = 0) -> tuple[ReversalRuntime, FakeTrader, Market]:
    settings = ReversalSettings(
        trigger_streak=4,
        stakes=SPARSE_RECOVERY_NOTIONALS,
        sparse_recovery_notional_stages=SPARSE_RECOVERY_NOTIONALS,
        full_loss_recovery_enabled=False,
    )
    strategy = ReversalV11(settings)
    for epoch in (100, 400, 700, 1000):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.UP)
    if failures:
        strategy.state.active_round = ActiveRound(
            round_id=1,
            trend_side=Direction.UP,
            failures=failures,
            cumulative_loss=Decimal("20"),
        )
    trader = FakeTrader()
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / f"sparse-{failures}.json",
        winner_lookup=winners,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )
    return runtime, trader, replace(MARKET, slug="btc-updown-5m-1300")


def test_sparse_profile_converts_stage_one_4_pusd_budget_to_shares(tmp_path) -> None:
    runtime, trader, market = sparse_runtime(tmp_path)

    result = runtime.tick(
        market=market,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "entry_complete"
    assert trader.orders == [("202", Decimal("0.44"), Decimal("9"), "FAK")]
    assert trader.orders[0][1] * trader.orders[0][2] == Decimal("3.96")


def test_sparse_profile_stage_eight_sizes_to_recover_all_prior_loss(tmp_path) -> None:
    runtime, trader, market = sparse_runtime(tmp_path, failures=7)

    result = runtime.tick(
        market=market,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "entry_complete"
    shares = trader.orders[0][2]
    fee = shares * Decimal("0.07") * Decimal("0.44") * Decimal("0.56")
    assert shares * Decimal("0.56") - fee >= Decimal("20")
    assert shares * Decimal("0.44") >= Decimal("15.90")


def test_fixed_notional_profile_requires_ask_strictly_below_049(tmp_path) -> None:
    runtime, trader, market = fixed_notional_runtime(tmp_path, "0.49")

    result = runtime.tick(
        market=market,
        up_book=UP_BOOK,
        down_book=book("202", "0.47", "0.49", "20"),
        health=HEALTHY,
    )

    assert result.status == "fixed_notional_entry_filtered"
    assert trader.orders == []
    active = runtime.strategy.state.active_round
    assert active is not None
    assert active.failures == 0
    assert active.awaiting_window is None


def test_fixed_notional_recovery_stage_bypasses_ask_filter_and_recovers_segment(tmp_path) -> None:
    settings = ReversalSettings(
        trigger_streak=2,
        stakes=TWO_WINDOW_FIXED_NOTIONALS,
        fixed_notional_stages=TWO_WINDOW_FIXED_NOTIONALS,
        fixed_notional_max_ask=Decimal("0.49"),
        fixed_notional_recovery_loss_start_attempt=6,
        fixed_notional_recovery_start_attempt=7,
        full_loss_recovery_enabled=False,
    )
    strategy = ReversalV11(settings)
    strategy.state.recent_slugs = [
        "btc-updown-5m-400",
        "btc-updown-5m-700",
    ]
    strategy.state.recent_results = [Direction.UP, Direction.UP]
    strategy.state.last_settled_slug = "btc-updown-5m-700"
    strategy.state.active_round = ActiveRound(
        round_id=1,
        trend_side=Direction.UP,
        failures=6,
        cumulative_loss=Decimal("5"),
    )
    trader = FakeTrader()
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "fixed-recovery.json",
        winner_lookup=winners,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )

    result = runtime.tick(
        market=replace(MARKET, slug="btc-updown-5m-1000"),
        up_book=UP_BOOK,
        down_book=book("202", "0.58", "0.60", "100"),
        health=HEALTHY,
    )

    assert result.status == "entry_complete"
    shares = trader.orders[0][2]
    fee = shares * Decimal("0.07") * Decimal("0.60") * Decimal("0.40")
    assert shares * Decimal("0.40") - fee >= Decimal("5")


def test_fixed_notional_recovery_ends_round_only_when_full_size_is_unfunded(tmp_path) -> None:
    settings = ReversalSettings(
        trigger_streak=2,
        stakes=TWO_WINDOW_FIXED_NOTIONALS,
        fixed_notional_stages=TWO_WINDOW_FIXED_NOTIONALS,
        fixed_notional_recovery_loss_start_attempt=6,
        fixed_notional_recovery_start_attempt=7,
        continue_final_stage_until_success_or_unfunded=True,
        full_loss_recovery_enabled=False,
    )
    strategy = ReversalV11(settings)
    strategy.state.recent_slugs = [
        "btc-updown-5m-400",
        "btc-updown-5m-700",
    ]
    strategy.state.recent_results = [Direction.UP, Direction.UP]
    strategy.state.last_settled_slug = "btc-updown-5m-700"
    strategy.state.active_round = ActiveRound(
        round_id=1,
        trend_side=Direction.UP,
        failures=6,
        cumulative_loss=Decimal("20"),
    )
    trader = FakeTrader()
    trader.collateral = Decimal("2")
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "fixed-unfunded.json",
        winner_lookup=winners,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )

    result = runtime.tick(
        market=replace(MARKET, slug="btc-updown-5m-1000"),
        up_book=UP_BOOK,
        down_book=book("202", "0.58", "0.60", "100"),
        health=HEALTHY,
    )

    assert result.status == "break_even_target_unfunded"
    assert trader.orders == []
    assert strategy.state.active_round is None
    assert strategy.state.blocked_trend_side is Direction.UP


def test_round_loss_hard_cap_blocks_order_before_submission(tmp_path) -> None:
    settings = ReversalSettings(
        trigger_streak=4,
        allocated_capital=Decimal("64"),
        maximum_streak_loss=Decimal("64"),
        hard_round_loss_limit=Decimal("64"),
    )
    strategy = ReversalV11(settings)
    strategy.state.active_round = ActiveRound(
        round_id=1,
        trend_side=Direction.UP,
        failures=4,
        awaiting_window=MARKET.slug,
        execution_phase="planned",
        planned_shares=Decimal("16"),
        cumulative_loss=Decimal("63"),
    )
    plan = TradePlan(
        round_id=1,
        window_slug=MARKET.slug,
        attempt=5,
        making_amount=Decimal("16"),
        trend_side=Direction.UP,
        retained_side=Direction.DOWN,
    )
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=winners,
        splitter=None,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )

    reason = runtime._round_loss_entry_block_reason(plan=plan, book=DOWN_BOOK)

    assert reason is not None
    assert "exceeds hard limit 64" in reason


def test_round_loss_hard_cap_ends_round_without_submitting_order(tmp_path) -> None:
    settings = ReversalSettings(
        trigger_streak=4,
        allocated_capital=Decimal("64"),
        maximum_streak_loss=Decimal("64"),
        hard_round_loss_limit=Decimal("64"),
    )
    strategy = ReversalV11(settings)
    strategy.state.active_round = ActiveRound(
        round_id=1,
        trend_side=Direction.UP,
        failures=4,
        cumulative_loss=Decimal("63"),
    )
    trader = FakeTrader()
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
    strategy.state.recent_slugs = [
        "btc-updown-5m--500",
        "btc-updown-5m--200",
        "btc-updown-5m-100",
        "btc-updown-5m-400",
    ]
    strategy.state.recent_results = [Direction.UP] * 4

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert result.status == "round_loss_limit_reached"
    assert result.detail is not None and "exceeds hard limit 64" in result.detail
    assert trader.orders == []
    assert strategy.state.active_round is None


def test_soft_limit_final_recovery_can_submit_more_than_64_pusd(tmp_path) -> None:
    settings = ReversalSettings(
        trigger_streak=4,
        hard_round_loss_limit=None,
        soft_round_loss_limit=Decimal("64"),
        one_final_recovery_after_soft_limit=True,
        full_loss_recovery_start_attempt=2,
        full_loss_recovery_strict_funding=True,
    )
    strategy = ReversalV11(settings)
    strategy.state.active_round = ActiveRound(
        round_id=1,
        trend_side=Direction.UP,
        failures=6,
        cumulative_loss=Decimal("64"),
    )
    strategy.state.recent_slugs = [
        "btc-updown-5m--500",
        "btc-updown-5m--200",
        "btc-updown-5m-100",
        "btc-updown-5m-400",
    ]
    strategy.state.recent_results = [Direction.UP] * 4
    trader = FakeTrader()
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "soft-limit.json",
        winner_lookup=winners,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=book("202", "0.49", "0.50", "500"),
        health=HEALTHY,
    )

    assert result.status == "entry_complete"
    price, shares = trader.orders[0][1:3]
    cost = price * shares
    fee = shares * Decimal("0.07") * price * (Decimal("1") - price)
    assert cost > Decimal("64")
    assert shares * (Decimal("1") - price) - fee >= Decimal("64")
    assert result.order is not None
    assert result.order["soft_limit_final_recovery"] is True


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


def first_stage_retry_runtime(tmp_path, trader) -> tuple[ReversalRuntime, Market]:
    settings = ReversalSettings(
        trigger_streak=4,
        stakes=FIRST_STAGE_ONLY_STAKES,
        first_stage_only_enabled=True,
        full_loss_recovery_enabled=False,
    )
    runtime = ReversalRuntime(
        strategy=ReversalV11(settings),
        state_path=tmp_path / "first-stage-retry.json",
        winner_lookup=lambda slug: None,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )
    observed = runtime.observe_chainlink_open_prices(
        {
            f"btc-updown-5m-{epoch}": Decimal(str(index))
            for index, epoch in enumerate((100, 400, 700, 1000, 1300), start=1)
        }
    )
    assert len(observed) == 4
    return runtime, replace(MARKET, slug="btc-updown-5m-1300")


def test_first_stage_fak_zero_fill_retries_and_then_fills(tmp_path) -> None:
    trader = FailOnceBuyTrader()
    runtime, market = first_stage_retry_runtime(tmp_path, trader)

    first = runtime.tick(
        market=market,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )
    assert first.status == "entry_unmatched"
    assert runtime.strategy.state.active_round is not None
    assert runtime.strategy.state.active_round.entry_unmatched_attempts == 1
    assert runtime.strategy.state.blocked_trend_side is None

    second = runtime.tick(
        market=market,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )

    assert second.status == "entry_complete"
    assert len(trader.orders) == 2
    assert runtime.strategy.state.active_round is not None
    assert runtime.strategy.state.active_round.execution_phase == "direct_entry_complete"


def test_first_stage_uses_refreshed_executable_ask_immediately_before_fak(tmp_path) -> None:
    trader = FakeTrader()
    runtime, market = first_stage_retry_runtime(tmp_path, trader)
    refreshed_down = book("202", "0.51", "0.52", "20")

    result = runtime.tick(
        market=market,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
        book_refresh=lambda: (UP_BOOK, refreshed_down),
    )

    assert result.status == "entry_complete"
    assert trader.orders == [("202", Decimal("0.52"), Decimal("2.00"), "FAK")]


def test_first_stage_fak_retry_exhaustion_does_not_lock_trend(tmp_path) -> None:
    trader = BuyErrorTrader(
        RuntimeError(
            "no orders found to match with FAK order. FAK orders are "
            "partially filled or killed if no match is found."
        )
    )
    runtime, market = first_stage_retry_runtime(tmp_path, trader)

    results = [
        runtime.tick(
            market=market,
            up_book=UP_BOOK,
            down_book=DOWN_BOOK,
            health=HEALTHY,
        )
        for _ in range(3)
    ]

    assert [result.status for result in results] == [
        "entry_unmatched",
        "entry_unmatched",
        "first_stage_fak_skipped",
    ]
    assert len(trader.orders) == 3
    assert runtime.strategy.state.active_round is None
    assert runtime.strategy.state.blocked_trend_side is None

    runtime.strategy.settle_window(market.slug, Direction.UP)
    next_plan = runtime.strategy.plan_window("btc-updown-5m-1600", HEALTHY)
    assert next_plan is not None
    assert next_plan.attempt == 1


def test_first_stage_fak_retry_rechecks_price_without_locking_trend(tmp_path) -> None:
    trader = FailOnceBuyTrader()
    runtime, market = first_stage_retry_runtime(tmp_path, trader)

    first = runtime.tick(
        market=market,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )
    assert first.status == "entry_unmatched"

    filtered = runtime.tick(
        market=market,
        up_book=UP_BOOK,
        down_book=book("202", "0.63", "0.65", "20"),
        health=HEALTHY,
    )

    assert filtered.status == "first_stage_entry_filtered"
    assert "retry stopped" in (filtered.detail or "")
    assert len(trader.orders) == 1
    assert runtime.strategy.state.active_round is None
    assert runtime.strategy.state.blocked_trend_side is None


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
        failures=5,
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
    assert result.plan.attempt == 6
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
    assert trader.orders == [("101", Decimal("0.61"), Decimal("1"), "FAK")]


def completed_first_attempt() -> ReversalV11:
    strategy = ReversalV11()
    strategy.settle_window("btc-updown-5m-100", "UP")
    strategy.settle_window("btc-updown-5m-400", "UP")
    prior = strategy.plan_window(MARKET.slug, HEALTHY)
    assert prior is not None
    strategy.mark_split_submitting(prior)
    strategy.mark_split_confirmed(prior, "0xprior")
    strategy.record_exit_fill(prior, shares=Decimal("1"), proceeds=Decimal("0.50"))
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
    assert splitter.amounts == [Decimal("2")]
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
    assert trader.orders == [("101", Decimal("0.56"), Decimal("2"), "FAK")]


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


def test_twenty_fifth_attempt_never_presplits_a_twenty_sixth_stage(tmp_path) -> None:
    strategy = ReversalV11()
    strategy.state.active_round = ActiveRound(
        round_id=1,
        trend_side=Direction.UP,
        failures=24,
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


def test_gamma_backfill_restores_missing_four_streak_before_order(tmp_path) -> None:
    market = replace(
        MARKET,
        slug="btc-updown-5m-1300",
        token_ids=("202", "101"),
    )
    gamma_calls: list[str] = []

    def finalized_gamma(slug: str) -> str | None:
        gamma_calls.append(slug)
        return "DOWN" if slug == "btc-updown-5m-100" else None

    splitter = FakeSplitter()
    strategy = ReversalV11(ReversalSettings(trigger_streak=4))
    strategy.settle_window("btc-updown-5m-400", Direction.DOWN)
    strategy.settle_window("btc-updown-5m-700", Direction.DOWN)
    strategy.settle_window("btc-updown-5m-1000", Direction.DOWN)
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=finalized_gamma,
        splitter=splitter,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )

    backfilled = runtime.backfill_immediate_gamma_results(market.slug)

    assert backfilled == [("btc-updown-5m-100", "DOWN")]
    assert gamma_calls == ["btc-updown-5m-100"]
    assert runtime.strategy.state.recent_slugs == [
        "btc-updown-5m-100",
        "btc-updown-5m-400",
        "btc-updown-5m-700",
        "btc-updown-5m-1000",
    ]
    result = runtime.tick(
        market=market,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )
    assert result.status == "exit_complete"
    assert result.plan is not None
    assert result.plan.trend_side is Direction.DOWN
    assert result.plan.retained_side is Direction.UP
    assert splitter.calls == 1


def test_gamma_backfill_keeps_window_waiting_until_result_is_final(tmp_path) -> None:
    strategy = ReversalV11(ReversalSettings(trigger_streak=4))
    strategy.settle_window("btc-updown-5m-400", Direction.DOWN)
    strategy.settle_window("btc-updown-5m-700", Direction.DOWN)
    strategy.settle_window("btc-updown-5m-1000", Direction.DOWN)
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=lambda slug: None,
        splitter=FakeSplitter(),
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )

    assert runtime.backfill_immediate_gamma_results("btc-updown-5m-1300") == []
    assert runtime.strategy.state.recent_slugs == [
        "btc-updown-5m-400",
        "btc-updown-5m-700",
        "btc-updown-5m-1000",
    ]
    waiting = runtime.tick(
        market=replace(MARKET, slug="btc-updown-5m-1300"),
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
    )
    assert waiting.status == "waiting_chainlink_boundary_no_split"


def test_polygon_backfill_reconstructs_exact_trigger_history(tmp_path) -> None:
    outcomes = {
        "btc-updown-5m-100": "DOWN",
        "btc-updown-5m-400": "DOWN",
        "btc-updown-5m-700": "DOWN",
        "btc-updown-5m-1000": "DOWN",
    }
    runtime = ReversalRuntime(
        strategy=ReversalV11(ReversalSettings(trigger_streak=4)),
        state_path=tmp_path / "state.json",
        winner_lookup=lambda slug: None,
        chain_winner_lookup=outcomes.get,
        splitter=None,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )

    backfilled = runtime.backfill_immediate_chain_results("btc-updown-5m-1300")

    assert backfilled == list(outcomes.items())
    assert runtime.strategy.state.recent_slugs == list(outcomes)
    assert runtime.strategy.state.recent_results == [Direction.DOWN] * 4
    assert runtime.strategy.state.current_streak == 4
    assert runtime.strategy.state.last_settled_slug == "btc-updown-5m-1000"
    assert set(runtime.strategy.state.chain_verified_slugs) == set(outcomes)
    assert runtime.strategy.state.pending_gamma_results == {
        slug: Direction.DOWN for slug in outcomes
    }


def test_polygon_backfill_waits_when_any_predecessor_is_unresolved(tmp_path) -> None:
    outcomes = {
        "btc-updown-5m-100": "DOWN",
        "btc-updown-5m-400": "DOWN",
        "btc-updown-5m-700": None,
        "btc-updown-5m-1000": "DOWN",
    }
    runtime = ReversalRuntime(
        strategy=ReversalV11(ReversalSettings(trigger_streak=4)),
        state_path=tmp_path / "state.json",
        winner_lookup=lambda slug: None,
        chain_winner_lookup=outcomes.get,
        splitter=None,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )

    assert runtime.backfill_immediate_chain_results("btc-updown-5m-1300") == []
    assert runtime.strategy.state.recent_slugs == []


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
    assert restored.state.recent_results == [Direction.DOWN, Direction.UP]
    assert restored.state.current_streak_side is Direction.UP
    assert restored.state.current_streak == 1
    assert restored.metrics().api_order_errors == 1


def test_historical_audit_pump_never_waits_for_network_and_caps_batch(tmp_path) -> None:
    lookup_started = Event()
    release_lookup = Event()

    def slow_gamma(slug: str) -> str:
        del slug
        lookup_started.set()
        release_lookup.wait(1)
        return "UP"

    strategy = ReversalV11()
    strategy.state.pending_gamma_results = {
        f"btc-updown-5m-{epoch}": Direction.UP
        for epoch in (100, 400, 700)
    }
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=slow_gamma,
        splitter=None,
        trader=None,
        signature_type=3,
        live=False,
    )

    started_at = time.monotonic()
    assert runtime.pump_historical_audits(max_candidates=2) == []
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.05
    assert len(runtime._historical_audits_inflight) == 2
    assert lookup_started.wait(0.2)
    release_lookup.set()


def test_polygon_mismatch_is_applied_but_retained_for_gamma_audit(tmp_path) -> None:
    runtime = ReversalRuntime(
        strategy=ReversalV11(),
        state_path=tmp_path / "state.json",
        winner_lookup=lambda slug: None,
        chain_winner_lookup=lambda slug: "DOWN" if slug.endswith("100") else None,
        splitter=None,
        trader=FakeTrader(),
        signature_type=3,
        live=True,
    )
    seed_two_chainlink_up(runtime)

    with pytest.raises(ChainResultMismatch) as raised:
        runtime.verify_chain_results()
    runtime.quarantine_chain_mismatch(raised.value)

    restored = ReversalV11.load(tmp_path / "state.json")
    assert restored.state.pending_gamma_results["btc-updown-5m-100"] is Direction.DOWN
    assert restored.state.chain_verified_slugs == ["btc-updown-5m-100"]
    assert restored.state.chain_mismatch_slugs == ["btc-updown-5m-100"]
    assert restored.state.recent_results == [Direction.DOWN, Direction.UP]


def test_chain_correction_sells_only_and_never_replaces(tmp_path) -> None:
    strategy = ReversalV11(ReversalSettings(trigger_streak=2))
    strategy.state.recent_slugs = ["btc-updown-5m-100", "btc-updown-5m-400"]
    strategy.state.recent_results = [Direction.DOWN, Direction.DOWN]
    strategy.state.active_round = ActiveRound(
        round_id=10,
        trend_side=Direction.UP,
        awaiting_window="btc-updown-5m-700",
        execution_phase="direct_entry_complete",
        split_transaction_hash="direct-buy",
        exit_sold_shares=Decimal("5"),
        exit_sell_proceeds=Decimal("2.90"),
        planned_shares=Decimal("5"),
    )
    class ChainCorrectionTrader(FakeTrader):
        def conditional_balance(self, token_id, signature_type):
            del token_id, signature_type
            return Decimal("5")

    trader = ChainCorrectionTrader()
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=lambda slug: None,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )

    result = runtime.correct_gamma_mismatch_position(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        seconds_left=Decimal("1"),
        source="chain",
        allow_replacement=False,
    )

    assert result.status == "chain_position_closed"
    assert len(trader.orders) == 1
    assert trader.orders[0][0] == "202"
    assert runtime.strategy.state.active_round is None


def test_gamma_correction_replaces_filled_direct_order_without_extra_loss_budget(
    tmp_path,
) -> None:
    class CorrectionTrader(FakeTrader):
        def __init__(self) -> None:
            super().__init__()
            self.balance = Decimal("5")

        def conditional_balance(self, token_id, signature_type):
            assert token_id == "202"
            assert signature_type == 3
            return self.balance

        def sell_limit(
            self, token_id, price, size, tick_size, neg_risk, order_type="GTC", **kwargs
        ):
            self.orders.append(("SELL", token_id, price, size, order_type))
            self.balance -= size
            return {
                "status": "matched",
                "makingAmount": str(size),
                "takingAmount": str(size * price),
            }

        def buy_limit(
            self, token_id, price, size, tick_size, neg_risk, order_type="GTC", **kwargs
        ):
            self.orders.append(("BUY", token_id, price, size, order_type))
            return {
                "status": "matched",
                "makingAmount": str(size * price),
                "takingAmount": str(size),
            }

    strategy = ReversalV11(ReversalSettings(trigger_streak=4))
    strategy.state.recent_slugs = [
        "btc-updown-5m-100",
        "btc-updown-5m-400",
        "btc-updown-5m-700",
        "btc-updown-5m-1000",
    ]
    strategy.state.recent_results = [Direction.DOWN] * 4
    strategy.state.active_round = ActiveRound(
        round_id=9,
        trend_side=Direction.UP,
        awaiting_window="btc-updown-5m-1300",
        execution_phase="direct_entry_complete",
        split_transaction_hash="direct-buy",
        exit_sold_shares=Decimal("5"),
        exit_sell_proceeds=Decimal("2.90"),
        planned_shares=Decimal("5"),
    )
    trader = CorrectionTrader()
    recorded_orders: list[dict] = []
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=lambda slug: None,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
        order_callback=recorded_orders.append,
    )

    result = runtime.correct_gamma_mismatch_position(
        market=replace(MARKET, slug="btc-updown-5m-1300"),
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        seconds_left=Decimal("120"),
    )

    assert result.status == "gamma_order_replaced"
    assert trader.orders[0][:2] == ("SELL", "202")
    assert trader.orders[1][:2] == ("BUY", "101")
    assert [order["order_role"] for order in recorded_orders] == [
        "gamma_correction_exit",
        "gamma_correction_replacement",
    ]
    active = runtime.strategy.state.active_round
    assert active is not None
    assert active.trend_side is Direction.DOWN
    assert active.target_side is Direction.UP
    sell_proceeds = Decimal(recorded_orders[0]["response"]["takingAmount"])
    buy_cost = Decimal(recorded_orders[1]["response"]["makingAmount"])
    assert buy_cost + active.entry_fees <= sell_proceeds


def test_gamma_correction_blocks_expensive_replacement_before_selling(tmp_path) -> None:
    strategy = ReversalV11(ReversalSettings(trigger_streak=4))
    strategy.state.recent_slugs = [
        "btc-updown-5m-100",
        "btc-updown-5m-400",
        "btc-updown-5m-700",
        "btc-updown-5m-1000",
    ]
    strategy.state.recent_results = [Direction.DOWN] * 4
    strategy.state.active_round = ActiveRound(
        round_id=10,
        trend_side=Direction.UP,
        awaiting_window="btc-updown-5m-1300",
        execution_phase="direct_entry_complete",
        split_transaction_hash="direct-buy",
        exit_sold_shares=Decimal("5"),
        exit_sell_proceeds=Decimal("2.90"),
        planned_shares=Decimal("5"),
    )

    class NoOrderTrader(FakeTrader):
        def conditional_balance(self, token_id, signature_type):
            return Decimal("5")

    trader = NoOrderTrader()
    runtime = ReversalRuntime(
        strategy=strategy,
        state_path=tmp_path / "state.json",
        winner_lookup=lambda slug: None,
        splitter=None,
        trader=trader,
        signature_type=3,
        live=True,
        execution_mode="direct_buy",
    )

    result = runtime.correct_gamma_mismatch_position(
        market=replace(MARKET, slug="btc-updown-5m-1300"),
        up_book=book("101", "0.18", "0.81", "20"),
        down_book=DOWN_BOOK,
        seconds_left=Decimal("120"),
    )

    assert result.status == "gamma_correction_blocked"
    assert "0.80" in (result.detail or "")
    assert trader.orders == []


def test_confirmed_legacy_unused_split_is_merged_for_recovery(tmp_path) -> None:
    strategy = ReversalV11()
    prepared = strategy.prepare_opening_split(MARKET.slug)
    strategy.mark_opening_split_submitting()
    strategy.mark_opening_split_confirmed("0xlegacy")
    assert prepared.amount == Decimal("1")
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


def test_paused_control_mode_allows_low_balance_but_keeps_api_checks() -> None:
    class TraderForCheck:
        def open_orders(self):
            return []

        def conditional_balance(self, token_id, signature_type):
            return Decimal("0")

        def collateral_balance(self, signature_type):
            return Decimal("0.94")

    report = reversal_startup_self_check(
        market=MARKET,
        splitter=None,
        trader=TraderForCheck(),
        signature_type=3,
        execution_mode="direct_buy",
        wallet="0xfunder",
        require_trade_collateral=False,
    )

    assert report.collateral_units == 940_000
    assert report.open_orders == 0


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
