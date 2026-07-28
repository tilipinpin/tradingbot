from datetime import date
from decimal import Decimal

from src.reversal_v11 import (
    Direction,
    MarketHealth,
    ReversalSettings,
    ReversalV11,
    build_exit_sequence,
    format_daily_report,
)


HEALTHY = MarketHealth(
    short_volatility=Decimal("0.001"),
    absolute_window_move=Decimal("0.002"),
    trend_bid_depth=Decimal("20"),
    trend_spread=Decimal("0.02"),
    estimated_sellable=True,
)


def seed_two_up(strategy: ReversalV11) -> None:
    strategy.settle_window("btc-updown-5m-100", Direction.UP)
    strategy.settle_window("btc-updown-5m-400", Direction.UP)


def test_two_same_results_trigger_reversal_target() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)

    plan = strategy.plan_window("btc-updown-5m-700", HEALTHY)

    assert plan is not None
    assert plan.attempt == 1
    assert plan.making_amount == Decimal("2")
    assert plan.trend_side is Direction.UP
    assert plan.retained_side is Direction.DOWN


def test_opening_split_is_prepared_before_results_and_persists(tmp_path) -> None:
    strategy = ReversalV11()
    prepared = strategy.prepare_opening_split("btc-updown-5m-700")
    assert prepared.amount == Decimal("2")
    strategy.mark_opening_split_submitting()
    strategy.mark_opening_split_confirmed("0xopening")
    state_path = tmp_path / "opening.json"
    strategy.dump(state_path)

    restored = ReversalV11.load(state_path)
    assert restored.state.prepared_split is not None
    assert restored.state.prepared_split.window_slug == "btc-updown-5m-700"
    assert restored.state.prepared_split.execution_phase == "split_confirmed"


def test_next_opening_split_prepares_next_martingale_stage_before_result() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    prior = strategy.plan_window("btc-updown-5m-700", HEALTHY)
    assert prior is not None
    strategy.mark_split_submitting(prior)
    strategy.mark_split_confirmed(prior, "0xprior")
    strategy.record_exit_fill(prior, shares=Decimal("2"), proceeds=Decimal("1"))

    assert strategy.opening_split_amount("btc-updown-5m-1000") == Decimal("4")


def test_next_opening_split_is_blocked_until_prior_trend_exit_is_complete() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    prior = strategy.plan_window("btc-updown-5m-700", HEALTHY)
    assert prior is not None
    strategy.mark_split_submitting(prior)
    strategy.mark_split_confirmed(prior, "0xprior")

    try:
        strategy.opening_split_amount("btc-updown-5m-1000")
    except RuntimeError as exc:
        assert "not fully exited" in str(exc)
    else:
        raise AssertionError("next opening split should be blocked")


def test_progression_is_2_4_8_16_then_forced_exit_and_immediate_retrigger() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    amounts = []
    for index, slug in enumerate(("700", "1000", "1300", "1600"), start=1):
        plan = strategy.plan_window(f"btc-updown-5m-{slug}", HEALTHY)
        assert plan is not None
        amounts.append(plan.making_amount)
        status = strategy.settle_window(f"btc-updown-5m-{slug}", Direction.UP)
        assert status == (
            "forced_exit_after_four_failures" if index == 4 else "trend_continued"
        )

    assert amounts == [Decimal("2"), Decimal("4"), Decimal("8"), Decimal("16")]
    assert strategy.metrics().total_making_amount == Decimal("30")
    assert strategy.metrics().forced_exit_rounds == 1
    restarted = strategy.plan_window("btc-updown-5m-1900", HEALTHY)
    assert restarted is not None
    assert restarted.making_amount == Decimal("2")
    assert restarted.round_id == 2


def test_reversal_success_ends_round_and_records_stage() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    first = strategy.plan_window("btc-updown-5m-700", HEALTHY)
    assert first is not None

    status = strategy.settle_window(first.window_slug, Direction.DOWN)

    assert status == "reversal_success"
    assert strategy.state.active_round is None
    assert strategy.metrics().successful_rounds == 1
    assert strategy.metrics().stage_successes["1"] == 1
    assert strategy.metrics().settlement_payout == Decimal("2")


def test_filter_pauses_without_consuming_attempt() -> None:
    strategy = ReversalV11(ReversalSettings(market_filters_enabled=True))
    seed_two_up(strategy)
    unhealthy = MarketHealth(
        short_volatility=Decimal("0.002"),
        absolute_window_move=Decimal("0.004"),
        trend_bid_depth=Decimal("1"),
        trend_spread=Decimal("0.08"),
        estimated_sellable=False,
    )

    assert strategy.plan_window("btc-updown-5m-700", unhealthy) is None
    strategy.settle_window("btc-updown-5m-700", Direction.UP)
    resumed = strategy.plan_window("btc-updown-5m-1000", HEALTHY)

    assert resumed is not None
    assert resumed.attempt == 1
    assert resumed.making_amount == Decimal("2")
    assert strategy.metrics().volatility_pauses == 1


def test_direct_exit_mode_does_not_block_on_market_health() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    unhealthy = MarketHealth(
        short_volatility=Decimal("1"),
        absolute_window_move=Decimal("1"),
        trend_bid_depth=Decimal("0"),
        trend_spread=Decimal("1"),
        estimated_sellable=False,
        market_data_ok=False,
        trading_api_ok=False,
    )

    assert strategy.plan_window("btc-updown-5m-700", unhealthy) is not None


def test_exit_sequence_prefers_half_then_reprices_and_finishes_fak() -> None:
    high = build_exit_sequence(Decimal("0.56"), Decimal("0.01"))
    low = build_exit_sequence(Decimal("0.42"), Decimal("0.01"))

    assert high[0].limit_price == Decimal("0.56")
    assert high[0].order_type == "FAK"
    assert low[0].limit_price == Decimal("0.50")
    assert low[0].order_type == "GTC"
    assert low[-1].limit_price == Decimal("0.41")
    assert low[-1].order_type == "FAK"


def test_daily_report_contains_required_v11_fields() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    strategy.record_execution(
        sell_proceeds=Decimal("1.10"),
        settlement_payout=Decimal("2"),
        fees=Decimal("0.02"),
        slippage=Decimal("0.01"),
        unmatched_orders=1,
        api_order_errors=2,
        equity_drawdown=Decimal("0.50"),
    )

    report = format_daily_report(date.today(), strategy.metrics())

    for label in (
        "总结算窗口",
        "触发轮数",
        "四次失败退出",
        "各阶段成功",
        "最大连续同向窗口",
        "总做市金额",
        "卖出回款",
        "结算收益",
        "手续费",
        "滑点",
        "未成交订单",
        "当日净收益",
        "当日最大回撤",
        "波动率暂停",
        "API及下单异常",
    ):
        assert label in report


def test_state_round_trip_preserves_active_attempt(tmp_path) -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    first = strategy.plan_window("btc-updown-5m-700", HEALTHY)
    assert first is not None
    strategy.settle_window(first.window_slug, Direction.UP)
    state_path = tmp_path / "reversal-v11.json"
    strategy.dump(state_path)

    restored = ReversalV11.load(state_path)
    second = restored.plan_window("btc-updown-5m-1000", HEALTHY)

    assert second is not None
    assert second.attempt == 2
    assert second.making_amount == Decimal("4")


def test_execute_complete_set_passes_active_attempt_amount_to_splitter() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    plan = strategy.plan_window("btc-updown-5m-700", HEALTHY)
    assert plan is not None

    class FakeSplitter:
        def split(self, **kwargs):
            assert kwargs["amount"] == Decimal("2")
            assert kwargs["neg_risk"] is False
            return "confirmed"

    result = strategy.execute_complete_set(
        plan,
        FakeSplitter(),
        condition_id="0x" + "22" * 32,
        up_token_id="101",
        down_token_id="202",
        neg_risk=False,
    )

    assert result == "confirmed"
