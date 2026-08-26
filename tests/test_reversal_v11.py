import json
from datetime import date
from decimal import Decimal

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
    assert plan.making_amount == Decimal("1")
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
    assert plan.making_amount == Decimal("1")
    assert plan.trend_side is Direction.UP
    assert plan.retained_side is Direction.DOWN


def test_first_stage_only_profile_ends_after_its_single_loss() -> None:
    strategy = ReversalV11(
        ReversalSettings(
            trigger_streak=4,
            stakes=FIRST_STAGE_ONLY_STAKES,
            first_stage_only_enabled=True,
            full_loss_recovery_enabled=False,
        )
    )
    for epoch in (100, 400, 700, 1000):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.UP)

    plan = strategy.plan_window("btc-updown-5m-1300", HEALTHY)

    assert plan is not None
    assert plan.making_amount == Decimal("1")
    assert strategy.settle_window(plan.window_slug, Direction.UP) == "first_stage_only_loss"
    assert strategy.state.active_round is None
    assert strategy.state.blocked_trend_side is Direction.UP
    assert strategy.plan_window("btc-updown-5m-1600", HEALTHY) is None


def test_stale_direct_entry_success_is_closed_without_catchup_trade() -> None:
    strategy = ReversalV11(ReversalSettings(trigger_streak=4))
    strategy.state.active_round = ActiveRound(
        round_id=9,
        trend_side=Direction.UP,
        failures=1,
        awaiting_window="btc-updown-5m-1300",
        execution_phase="direct_entry_complete",
        split_transaction_hash="direct-buy",
        exit_sold_shares=Decimal("2.5"),
    )
    strategy.state.last_settled_slug = "btc-updown-5m-2500"

    outcome = strategy.reconcile_stale_direct_entry(
        "btc-updown-5m-1300", Direction.DOWN
    )

    assert outcome == "stale_reversal_success"
    assert strategy.state.active_round is None
    assert strategy.metrics().successful_rounds == 1
    assert strategy.metrics().stage_successes["2"] == 1
    assert strategy.metrics().settlement_payout == Decimal("2.5")


def compact_strategy() -> ReversalV11:
    return ReversalV11(
        ReversalSettings(
            trigger_streak=4,
            stakes=COMPACT_REVERSAL_STAKES,
            compact_two_stage_enabled=True,
            full_loss_recovery_enabled=False,
        )
    )


def test_compact_profile_has_two_fixed_stages_and_pauses_after_five_losing_rounds() -> None:
    strategy = compact_strategy()
    strategy.state.compact_consecutive_losing_rounds = 4
    for epoch in (100, 400, 700, 1000):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.UP)

    first = strategy.plan_window("btc-updown-5m-1300", HEALTHY)
    assert first is not None and first.making_amount == Decimal("2")
    assert strategy.settle_window(first.window_slug, Direction.UP) == "trend_continued"
    second = strategy.plan_window("btc-updown-5m-1600", HEALTHY)
    assert second is not None and second.making_amount == Decimal("4")
    assert strategy.settle_window(second.window_slug, Direction.UP) == "compact_two_stage_loss"
    assert strategy.state.compact_pause_windows_remaining == 3

    for epoch in (1900, 2200, 2500):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.DOWN)
        assert strategy.plan_window(f"btc-updown-5m-{epoch + 300}", HEALTHY) is None
    strategy.settle_window("btc-updown-5m-2800", Direction.DOWN)
    resumed = strategy.plan_window("btc-updown-5m-3100", HEALTHY)
    assert resumed is not None and resumed.making_amount == Decimal("2")


def test_compact_stage_two_filter_counts_the_realized_stage_one_loss() -> None:
    strategy = compact_strategy()
    for epoch in (100, 400, 700, 1000):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.UP)

    first = strategy.plan_window("btc-updown-5m-1300", HEALTHY)
    assert first is not None
    assert strategy.settle_window(first.window_slug, Direction.UP) == "trend_continued"
    second = strategy.plan_window("btc-updown-5m-1600", HEALTHY)
    assert second is not None
    strategy.abandon_filtered_attempt(second)

    assert strategy.state.compact_consecutive_losing_rounds == 1
    assert strategy.state.blocked_trend_side is Direction.UP


def test_three_streak_variant_trades_one_window_before_four_streak() -> None:
    strategy = ReversalV11(ReversalSettings(trigger_streak=3))
    for epoch in (100, 400):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.DOWN)

    assert strategy.plan_window("btc-updown-5m-700", HEALTHY) is None

    strategy.settle_window("btc-updown-5m-700", Direction.DOWN)
    plan = strategy.plan_window("btc-updown-5m-1000", HEALTHY)

    assert plan is not None
    assert plan.attempt == 1
    assert plan.making_amount == Decimal("1")
    assert plan.trend_side is Direction.DOWN
    assert plan.retained_side is Direction.UP


def test_two_window_fixed_notional_profile_has_twelve_stages() -> None:
    strategy = ReversalV11(
        ReversalSettings(
            trigger_streak=2,
            stakes=TWO_WINDOW_FIXED_NOTIONALS,
            fixed_notional_stages=TWO_WINDOW_FIXED_NOTIONALS,
            full_loss_recovery_enabled=False,
        )
    )
    for epoch in (100, 400):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.UP)

    first = strategy.plan_window("btc-updown-5m-700", HEALTHY)
    assert first is not None and first.making_amount == Decimal("1")
    assert strategy.settle_window(first.window_slug, Direction.UP) == "trend_continued"

    expected = TWO_WINDOW_FIXED_NOTIONALS[1:]
    for index, amount in enumerate(expected, start=1):
        plan = strategy.plan_window(f"btc-updown-5m-{700 + index * 300}", HEALTHY)
        assert plan is not None and plan.making_amount == amount
        outcome = strategy.settle_window(plan.window_slug, Direction.UP)
        assert outcome == (
            "fixed_notional_stages_loss"
            if index == len(expected)
            else "trend_continued"
        )
    assert strategy.state.active_round is None
    assert strategy.state.blocked_trend_side is Direction.UP


def test_two_window_recovery_segment_excludes_first_five_stage_losses() -> None:
    strategy = ReversalV11(
        ReversalSettings(
            trigger_streak=2,
            stakes=TWO_WINDOW_FIXED_NOTIONALS,
            fixed_notional_stages=TWO_WINDOW_FIXED_NOTIONALS,
            fixed_notional_recovery_loss_start_attempt=6,
            fixed_notional_recovery_start_attempt=7,
            full_loss_recovery_enabled=False,
        )
    )
    for epoch in (100, 400):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.UP)

    for attempt in range(1, 7):
        plan = strategy.plan_window(
            f"btc-updown-5m-{400 + attempt * 300}", HEALTHY
        )
        assert plan is not None and plan.attempt == attempt
        strategy.mark_direct_entry_ready(plan)
        strategy.record_direct_entry_fill(
            plan,
            shares=plan.making_amount,
            cost=plan.making_amount * Decimal("0.45"),
        )
        assert strategy.settle_window(plan.window_slug, Direction.UP) == "trend_continued"
        assert strategy.state.active_round is not None
        if attempt < 6:
            assert strategy.state.active_round.cumulative_loss == 0

    assert strategy.state.active_round.cumulative_loss > 0
    assert not strategy.settings.uses_fixed_notional_full_loss_recovery(6)
    assert strategy.settings.uses_fixed_notional_full_loss_recovery(7)


def test_funded_round_repeats_final_stage_until_reversal_success() -> None:
    strategy = ReversalV11(
        ReversalSettings(
            trigger_streak=2,
            stakes=TWO_WINDOW_FIXED_NOTIONALS,
            fixed_notional_stages=TWO_WINDOW_FIXED_NOTIONALS,
            fixed_notional_recovery_loss_start_attempt=6,
            fixed_notional_recovery_start_attempt=7,
            continue_final_stage_until_success_or_unfunded=True,
            full_loss_recovery_enabled=False,
        )
    )
    strategy.state.active_round = ActiveRound(
        round_id=1,
        trend_side=Direction.UP,
        failures=11,
        cumulative_loss=Decimal("20"),
    )

    failed = strategy.plan_window("btc-updown-5m-700", HEALTHY)
    assert failed is not None and failed.attempt == 12
    strategy.mark_direct_entry_ready(failed)
    strategy.record_direct_entry_fill(
        failed, shares=Decimal("64"), cost=Decimal("32")
    )
    assert (
        strategy.settle_window(failed.window_slug, Direction.UP)
        == "trend_continued_at_final_stage"
    )
    assert strategy.state.active_round is not None
    assert strategy.state.active_round.failures == 11

    retry = strategy.plan_window("btc-updown-5m-1000", HEALTHY)
    assert retry is not None and retry.attempt == 12
    strategy.mark_direct_entry_ready(retry)
    strategy.record_direct_entry_fill(
        retry, shares=Decimal("64"), cost=Decimal("32")
    )
    assert strategy.settle_window(retry.window_slug, Direction.DOWN) == "reversal_success"
    assert strategy.state.active_round is None


def test_sparse_recovery_zero_stages_observe_without_committing() -> None:
    strategy = ReversalV11(
        ReversalSettings(
            trigger_streak=4,
            stakes=SPARSE_RECOVERY_NOTIONALS,
            sparse_recovery_notional_stages=SPARSE_RECOVERY_NOTIONALS,
            full_loss_recovery_enabled=False,
        )
    )
    for epoch in (100, 400, 700, 1000):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.UP)

    first = strategy.plan_window("btc-updown-5m-1300", HEALTHY)
    assert first is not None and first.making_amount == Decimal("4")
    assert strategy.settle_window(first.window_slug, Direction.UP) == "trend_continued"

    second = strategy.plan_window("btc-updown-5m-1600", HEALTHY)
    assert second is not None and second.making_amount == 0
    strategy.mark_observation_stage(second)
    assert strategy.state.active_round is not None
    assert strategy.state.active_round.committed == Decimal("4")
    assert strategy.settle_window(second.window_slug, Direction.UP) == "trend_continued"

    third = strategy.plan_window("btc-updown-5m-1900", HEALTHY)
    assert third is not None and third.making_amount == 0
    strategy.mark_observation_stage(third)
    assert strategy.settle_window(third.window_slug, Direction.DOWN) == "reversal_success"
    assert strategy.state.active_round is None


def test_sparse_recovery_excludes_stage_one_and_counts_loss_from_stage_four() -> None:
    strategy = ReversalV11(
        ReversalSettings(
            trigger_streak=4,
            stakes=SPARSE_RECOVERY_NOTIONALS,
            sparse_recovery_notional_stages=SPARSE_RECOVERY_NOTIONALS,
            full_loss_recovery_enabled=False,
        )
    )
    for epoch in (100, 400, 700, 1000):
        strategy.settle_window(f"btc-updown-5m-{epoch}", Direction.UP)

    first = strategy.plan_window("btc-updown-5m-1300", HEALTHY)
    assert first is not None
    strategy.mark_direct_entry_ready(first)
    strategy.record_direct_entry_fill(
        first, shares=Decimal("4"), cost=Decimal("2")
    )
    assert strategy.settle_window(first.window_slug, Direction.UP) == "trend_continued"
    assert strategy.state.active_round is not None
    assert strategy.state.active_round.cumulative_loss == 0

    for epoch in (1600, 1900):
        observation = strategy.plan_window(f"btc-updown-5m-{epoch}", HEALTHY)
        assert observation is not None and observation.making_amount == 0
        strategy.mark_observation_stage(observation)
        assert strategy.settle_window(observation.window_slug, Direction.UP) == "trend_continued"

    fourth = strategy.plan_window("btc-updown-5m-2200", HEALTHY)
    assert fourth is not None and fourth.making_amount == Decimal("1")
    strategy.mark_direct_entry_ready(fourth)
    strategy.record_direct_entry_fill(
        fourth, shares=Decimal("1"), cost=Decimal("0.50")
    )
    assert strategy.settle_window(fourth.window_slug, Direction.UP) == "trend_continued"
    assert strategy.state.active_round is not None
    assert strategy.state.active_round.cumulative_loss > Decimal("0.50")
    assert strategy.settings.uses_sparse_full_loss_recovery(5)


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
    assert prepared.amount == Decimal("1")
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
    strategy.record_exit_fill(prior, shares=Decimal("1"), proceeds=Decimal("0.50"))

    assert strategy.opening_split_amount("btc-updown-5m-1000") == Decimal("2")


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


def test_progression_has_twenty_five_attempts_then_blocks_until_real_reversal() -> None:
    strategy = ReversalV11()
    seed_two_up(strategy)
    amounts = []
    for index in range(1, 26):
        slug = str(700 + (index - 1) * 300)
        plan = strategy.plan_window(f"btc-updown-5m-{slug}", HEALTHY)
        assert plan is not None
        amounts.append(plan.making_amount)
        status = strategy.settle_window(f"btc-updown-5m-{slug}", Direction.UP)
        assert status == (
            "forced_exit_after_twenty_five_failures"
            if index == 25
            else "trend_continued"
        )

    assert amounts == [
        Decimal("1"),
        Decimal("2"),
        Decimal("4"),
        Decimal("8"),
    ] + [Decimal("16")] * 21
    assert strategy.metrics().total_making_amount == Decimal("351")
    assert strategy.metrics().forced_exit_rounds == 1
    restarted = strategy.plan_window("btc-updown-5m-8200", HEALTHY)
    assert restarted is None

    strategy.settle_window("btc-updown-5m-8200", Direction.DOWN)
    strategy.settle_window("btc-updown-5m-8500", Direction.DOWN)
    restarted = strategy.plan_window("btc-updown-5m-8800", HEALTHY)
    assert restarted is not None
    assert restarted.making_amount == Decimal("1")
    assert restarted.round_id == 2


def test_configured_ten_stage_limit_forces_exit_after_stage_ten() -> None:
    strategy = ReversalV11(ReversalSettings(maximum_attempts=10))
    seed_two_up(strategy)

    for index in range(1, 11):
        slug = f"btc-updown-5m-{700 + (index - 1) * 300}"
        plan = strategy.plan_window(slug, HEALTHY)
        assert plan is not None and plan.attempt == index
        outcome = strategy.settle_window(slug, Direction.UP)
        assert outcome == (
            "forced_exit_after_configured_failures"
            if index == 10
            else "trend_continued"
        )

    assert strategy.state.active_round is None
    assert strategy.state.blocked_trend_side is Direction.UP


def test_standard_profile_can_reuse_first_stage_rules_only_on_attempt_one() -> None:
    settings = ReversalSettings(
        trigger_streak=4,
        maximum_attempts=10,
        first_attempt_uses_first_stage_rules=True,
    )

    assert settings.stakes[0] == Decimal("1")
    assert settings.uses_first_stage_order_rules(1)
    assert not settings.uses_first_stage_order_rules(2)


def test_soft_limit_allows_one_final_recovery_then_ends_on_loss() -> None:
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
        round_id=8,
        trend_side=Direction.UP,
        failures=6,
        cumulative_loss=Decimal("64"),
    )

    plan = strategy.plan_window("btc-updown-5m-1300", HEALTHY)

    assert plan is not None and plan.attempt == 7
    assert strategy.state.active_round is not None
    assert strategy.state.active_round.soft_limit_final_recovery
    strategy.mark_direct_entry_ready(plan)
    strategy.record_direct_entry_fill(
        plan,
        shares=Decimal("150"),
        cost=Decimal("75"),
    )
    assert (
        strategy.settle_window(plan.window_slug, Direction.UP)
        == "soft_limit_final_recovery_loss"
    )
    assert strategy.state.active_round is None
    assert strategy.state.blocked_trend_side is Direction.UP


def test_soft_loss_limit_ends_round_immediately_when_crossed() -> None:
    settings = ReversalSettings(
        trigger_streak=4,
        soft_round_loss_limit=Decimal("64"),
        end_round_at_soft_loss_limit=True,
        full_loss_recovery_start_attempt=2,
        full_loss_recovery_strict_funding=True,
    )
    strategy = ReversalV11(settings)
    strategy.state.active_round = ActiveRound(
        round_id=9,
        trend_side=Direction.UP,
        failures=1,
        cumulative_loss=Decimal("63"),
    )
    plan = strategy.plan_window("btc-updown-5m-1300", HEALTHY)
    assert plan is not None and plan.attempt == 2
    strategy.mark_direct_entry_ready(plan)
    strategy.record_direct_entry_fill(
        plan,
        shares=Decimal("2"),
        cost=Decimal("1"),
    )

    assert (
        strategy.settle_window(plan.window_slug, Direction.UP)
        == "soft_loss_limit_reached"
    )
    assert strategy.state.active_round is None
    assert strategy.state.blocked_trend_side is Direction.UP


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
    assert strategy.metrics().settlement_payout == Decimal("1")


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
    assert plan.making_amount == Decimal("1")


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
    assert resumed.making_amount == Decimal("1")
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


def test_decayed_rv300_does_not_block_first_stage() -> None:
    settings = ReversalSettings(
        first_stage_rv300_filter_enabled=True,
        first_stage_max_rv300=Decimal("0.0020"),
    )
    strategy = ReversalV11(settings)
    seed_two_up(strategy)
    extreme = MarketHealth(
        short_volatility=Decimal("0.0004"),
        absolute_window_move=Decimal("0"),
        trend_bid_depth=Decimal("20"),
        trend_spread=Decimal("0.01"),
        estimated_sellable=True,
        five_minute_volatility=Decimal("0.0025"),
    )

    assert strategy.plan_window("btc-updown-5m-700", extreme) is not None
    assert strategy.state.active_round is not None
    assert strategy.metrics().volatility_pauses == 0


def test_persistent_rv300_blocks_first_stage() -> None:
    settings = ReversalSettings(
        first_stage_rv300_filter_enabled=True,
        first_stage_max_rv300=Decimal("0.0020"),
    )
    strategy = ReversalV11(settings)
    seed_two_up(strategy)
    persistent = MarketHealth(
        short_volatility=Decimal("0.0009"),
        absolute_window_move=Decimal("0"),
        trend_bid_depth=Decimal("20"),
        trend_spread=Decimal("0.01"),
        estimated_sellable=True,
        five_minute_volatility=Decimal("0.0025"),
    )

    assert strategy.plan_window("btc-updown-5m-700", persistent) is None
    assert strategy.metrics().volatility_pauses == 1


def test_hard_extreme_rv300_blocks_even_after_rv60_decay() -> None:
    settings = ReversalSettings(
        first_stage_rv300_filter_enabled=True,
        first_stage_max_rv300=Decimal("0.0020"),
    )
    strategy = ReversalV11(settings)
    seed_two_up(strategy)
    hard_extreme = MarketHealth(
        short_volatility=Decimal("0.0001"),
        absolute_window_move=Decimal("0"),
        trend_bid_depth=Decimal("20"),
        trend_spread=Decimal("0.01"),
        estimated_sellable=True,
        five_minute_volatility=Decimal("0.0040"),
    )

    assert strategy.plan_window("btc-updown-5m-700", hard_extreme) is None
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
        "二十五次失败退出",
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
    assert second.making_amount == Decimal("2")


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
            assert kwargs["amount"] == Decimal("1")
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
