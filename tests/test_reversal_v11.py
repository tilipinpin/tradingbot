import json
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
    assert strategy.metrics().triggered_rounds == 1
    assert strategy.metrics().executed_rounds == 0


def test_four_streak_variant_requires_four_consecutive_same_results() -> None:
    strategy = ReversalV11(ReversalSettings(trigger_streak=4))
    for epoch in (100, 400, 700):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.UP)

    assert strategy.plan_window("btc-updown-5m-1000", HEALTHY) is None

    strategy.settle_window("btc-updown-5m-1000", Direction.UP)
    plan = strategy.plan_window("btc-updown-5m-1300", HEALTHY)

    assert plan is not None
    assert plan.attempt == 1
    assert plan.making_amount == Decimal("2")
    assert plan.trend_side is Direction.UP
    assert plan.retained_side is Direction.DOWN


def test_three_streak_variant_trades_one_window_before_four_streak() -> None:
    strategy = ReversalV11(ReversalSettings(trigger_streak=3))
    for epoch in (100, 400):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.DOWN)

    assert strategy.plan_window("btc-updown-5m-700", HEALTHY) is None

    strategy.settle_window("btc-updown-5m-700", Direction.DOWN)
    plan = strategy.plan_window("btc-updown-5m-1000", HEALTHY)

    assert plan is not None
    assert plan.attempt == 1
    assert plan.making_amount == Decimal("2")
    assert plan.trend_side is Direction.DOWN
    assert plan.retained_side is Direction.UP


def test_four_streak_variant_rejects_mixed_or_non_adjacent_windows() -> None:
    mixed = ReversalV11(ReversalSettings(trigger_streak=4))
    for epoch, result in zip(
        (100, 400, 700, 1000),
        (Direction.UP, Direction.UP, Direction.DOWN, Direction.UP),
    ):
        mixed.settle_window(f"btc-updown-5m-{epoch}", result)
    assert mixed.plan_window("btc-updown-5m-1300", HEALTHY) is None

    gapped = ReversalV11(ReversalSettings(trigger_streak=4))
    for epoch in (100, 400, 1000, 1300):
        gapped.settle_window(f"btc-updown-5m-{epoch}", Direction.DOWN)
    assert gapped.plan_window("btc-updown-5m-1600", HEALTHY) is None


def test_missing_window_resets_recent_results_and_streak() -> None:
    strategy = ReversalV11(ReversalSettings(trigger_streak=4))
    for epoch in (100, 400, 700):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.UP)

    strategy.settle_window("btc-updown-5m-1300", Direction.UP)

    assert strategy.state.recent_slugs == ["btc-updown-5m-1300"]
    assert strategy.state.recent_results == [Direction.UP]
    assert strategy.state.current_streak == 1
    assert strategy.plan_window("btc-updown-5m-1600", HEALTHY) is None


def test_load_repairs_persisted_history_that_crosses_missing_windows(tmp_path) -> None:
    state_path = tmp_path / "gapped.json"
    state_path.write_text(
        json.dumps(
            {
                "recent_slugs": [
                    "btc-updown-5m-100",
                    "btc-updown-5m-400",
                    "btc-updown-5m-700",
                    "btc-updown-5m-1300",
                ],
                "recent_results": ["UP", "UP", "UP", "UP"],
                "last_settled_slug": "btc-updown-5m-1300",
                "current_streak_side": "UP",
                "current_streak": 4,
            }
        ),
        encoding="utf-8",
    )

    restored = ReversalV11.load(state_path)

    assert restored.state.recent_slugs == ["btc-updown-5m-1300"]
    assert restored.state.recent_results == [Direction.UP]
    assert restored.state.current_streak == 1


def test_confirmed_first_split_counts_executed_round_once() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    plan = strategy.plan_window("btc-updown-5m-700", HEALTHY)
    assert plan is not None
    strategy.prepare_opening_split(plan.window_slug)
    strategy.mark_opening_split_submitting()
    strategy.mark_opening_split_confirmed("0xconfirmed")

    strategy.adopt_opening_split(plan)

    assert strategy.metrics().triggered_rounds == 1
    assert strategy.metrics().executed_rounds == 1


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


def test_uncertain_opening_only_blocks_its_own_window() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    plan = strategy.plan_window("btc-updown-5m-700", HEALTHY)
    assert plan is not None
    strategy.prepare_opening_split(plan.window_slug)
    strategy.mark_opening_split_submitting()
    strategy.mark_opening_split_uncertain()

    assert strategy.roll_forward_uncertain_opening(plan.window_slug) is None
    assert strategy.state.active_round is not None
    assert strategy.state.prepared_split is not None

    abandoned = strategy.roll_forward_uncertain_opening("btc-updown-5m-1000")

    assert abandoned == plan.window_slug
    assert strategy.state.active_round is None
    assert strategy.state.prepared_split is None
    assert strategy.state.last_opening_processed_slug == plan.window_slug
    assert strategy.metrics().api_order_errors == 1


def test_confirmed_opening_cannot_be_rolled_forward_as_uncertain() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    plan = strategy.plan_window("btc-updown-5m-700", HEALTHY)
    assert plan is not None
    strategy.prepare_opening_split(plan.window_slug)
    strategy.mark_opening_split_submitting()
    strategy.mark_opening_split_confirmed("0xconfirmed")

    assert strategy.roll_forward_uncertain_opening("btc-updown-5m-1000") is None
    assert strategy.state.active_round is not None
    assert strategy.state.prepared_split is not None


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


def test_progression_has_fifteen_attempts_then_blocks_until_real_reversal() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    amounts = []
    for index, slug in enumerate(
        (
            "700", "1000", "1300", "1600", "1900", "2200", "2500", "2800",
            "3100", "3400", "3700", "4000", "4300", "4600", "4900",
        ),
        start=1,
    ):
        plan = strategy.plan_window(f"btc-updown-5m-{slug}", HEALTHY)
        assert plan is not None
        amounts.append(plan.making_amount)
        status = strategy.settle_window(f"btc-updown-5m-{slug}", Direction.UP)
        assert status == (
            "forced_exit_after_fifteen_failures" if index == 15 else "trend_continued"
        )

    assert amounts == [
        Decimal("2"),
        Decimal("4"),
        Decimal("8"),
        Decimal("16"),
        Decimal("16"),
        Decimal("16"),
        Decimal("16"),
        Decimal("16"),
        Decimal("16"),
        Decimal("16"),
        Decimal("16"),
        Decimal("16"),
        Decimal("16"),
        Decimal("16"),
        Decimal("16"),
    ]
    assert strategy.metrics().total_making_amount == Decimal("206")
    assert strategy.metrics().forced_exit_rounds == 1
    restarted = strategy.plan_window("btc-updown-5m-5200", HEALTHY)
    assert restarted is None

    strategy.settle_window("btc-updown-5m-5200", Direction.DOWN)
    strategy.settle_window("btc-updown-5m-5500", Direction.DOWN)
    restarted = strategy.plan_window("btc-updown-5m-5800", HEALTHY)
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


def test_legacy_profit_pause_state_does_not_stop_new_rounds(tmp_path) -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    state_path = tmp_path / "profit-stop.json"
    strategy.dump(state_path)
    payload = json.loads(state_path.read_text())
    payload.update(
        {
            "profit_baseline_net_profit": "0",
            "profit_target_reached": True,
            "profit_pause_windows_remaining": 7,
            "last_profit_pause_slug": "btc-updown-5m-400",
        }
    )
    state_path.write_text(json.dumps(payload))

    restored = ReversalV11.load(state_path)
    plan = restored.plan_window("btc-updown-5m-700", HEALTHY)

    assert plan is not None
    assert plan.attempt == 1
    assert plan.making_amount == Decimal("2")


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


def test_extreme_rv60_blocks_only_first_stage() -> None:
    settings = ReversalSettings(
        first_stage_rv60_filter_enabled=True,
        first_stage_max_rv60=Decimal("0.0010"),
    )
    strategy = ReversalV11(settings)
    seed_two_up(strategy)
    extreme = MarketHealth(
        short_volatility=Decimal("0.0010"),
        absolute_window_move=Decimal("0"),
        trend_bid_depth=Decimal("20"),
        trend_spread=Decimal("0.01"),
        estimated_sellable=True,
    )

    assert strategy.plan_window("btc-updown-5m-700", extreme) is None
    assert strategy.state.active_round is None
    assert strategy.metrics().volatility_pauses == 1

    normal = MarketHealth(
        short_volatility=Decimal("0.0009"),
        absolute_window_move=Decimal("0"),
        trend_bid_depth=Decimal("20"),
        trend_spread=Decimal("0.01"),
        estimated_sellable=True,
    )
    plan = strategy.plan_window("btc-updown-5m-700", normal)
    assert plan is not None
    strategy.state.active_round.awaiting_window = "btc-updown-5m-700"
    strategy.state.active_round.failures = 1

    resumed = strategy.plan_window("btc-updown-5m-700", extreme)
    assert resumed is not None
    assert resumed.attempt == 2


def test_extreme_rv300_blocks_first_stage() -> None:
    settings = ReversalSettings(
        first_stage_rv300_filter_enabled=True,
        first_stage_max_rv300=Decimal("0.0020"),
    )
    strategy = ReversalV11(settings)
    seed_two_up(strategy)
    extreme = MarketHealth(
        short_volatility=Decimal("0"),
        absolute_window_move=Decimal("0"),
        trend_bid_depth=Decimal("20"),
        trend_spread=Decimal("0.01"),
        estimated_sellable=True,
        five_minute_volatility=Decimal("0.0020"),
    )

    assert strategy.plan_window("btc-updown-5m-700", extreme) is None
    assert strategy.state.active_round is None
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
        "信号触发轮数",
        "已确认拆分轮数",
        "十五次失败退出",
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


def test_direct_entry_preserves_price_improvement_extra_shares() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    plan = strategy.plan_window("btc-updown-5m-700", HEALTHY)
    assert plan is not None
    strategy.mark_direct_entry_ready(plan)

    complete = strategy.record_direct_entry_fill(
        plan,
        shares=Decimal("2.07843"),
        cost=Decimal("1.059999"),
    )

    assert complete is True
    assert strategy.state.active_round is not None
    assert strategy.state.active_round.exit_sold_shares == Decimal("2.07843")
    assert strategy.state.active_round.exit_sell_proceeds == Decimal("1.059999")
    assert strategy.metrics().total_making_amount == Decimal("1.059999")


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
