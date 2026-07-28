from decimal import Decimal
from datetime import date

import pytest

from src.polymarket import Market, OrderBookLevel, OrderBookSnapshot
from src.reversal_runtime import (
    ReversalRuntime,
    ReversalRuntimeError,
    market_health_from_books,
    previous_5m_slug,
    reversal_startup_self_check,
)
from src.reversal_v11 import Direction, MarketHealth, ReversalV11


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
        self.error = error

    def split(self, **kwargs):
        self.calls += 1
        assert kwargs["amount"] == Decimal("2")
        if self.error is not None:
            raise self.error
        return Receipt()

    def merge(self, **kwargs):
        self.merge_calls += 1
        assert kwargs["amount"] == Decimal("2")
        return Receipt()


class FakeTrader:
    def __init__(self) -> None:
        self.balance = Decimal("2")
        self.orders = []

    def conditional_balance(self, token_id, signature_type):
        assert token_id == "101"
        assert signature_type == 3
        return self.balance

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


def winners(slug: str) -> str | None:
    return {
        "btc-updown-5m-100": "UP",
        "btc-updown-5m-400": "UP",
    }.get(slug)


def test_previous_slug_is_exactly_one_five_minute_window() -> None:
    assert previous_5m_slug(MARKET.slug) == "btc-updown-5m-400"
    assert previous_5m_slug(MARKET.slug, 2) == "btc-updown-5m-100"


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

    result = runtime.tick(
        market=MARKET,
        up_book=UP_BOOK,
        down_book=DOWN_BOOK,
        health=HEALTHY,
        book_refresh=lambda: (refreshed_up, DOWN_BOOK),
    )

    assert result.status == "exit_complete"
    assert trader.orders == [("101", Decimal("0.61"), Decimal("2"), "FAK")]


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


def test_runtime_waits_for_result_without_split_and_skips_when_streak_is_absent(tmp_path) -> None:
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
    assert waiting.status == "waiting_result_no_split"
    assert splitter.calls == 0
    assert splitter.merge_calls == 0

    outcomes["btc-updown-5m-400"] = "DOWN"
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
        def read_only_self_check(self):
            return {"deployed": True}

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
