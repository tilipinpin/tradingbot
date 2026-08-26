from __future__ import annotations

import json
import time
from decimal import Decimal

from src.fast_directional_hedge import (
    FastDirectionalHedgeEngine,
    FastDirectionalHedgeSettings,
    calibrate_probability,
)
from src.polymarket import OrderBookLevel, OrderBookSnapshot


def book(
    token: str,
    bid: str,
    ask: str,
    *,
    observed_at: float | None = None,
) -> OrderBookSnapshot:
    timestamp = observed_at if observed_at is not None else time.time()
    return OrderBookSnapshot(
        token_id=token,
        timestamp=str(int(timestamp * 1000)),
        bids=(OrderBookLevel(Decimal(bid), Decimal("20")),),
        asks=(OrderBookLevel(Decimal(ask), Decimal("20")),),
        minimum_order_size=Decimal("1"),
    )


def evaluate(
    engine: FastDirectionalHedgeEngine,
    *,
    probability_up: str = "0.80",
    up_ask: str = "0.50",
    down_ask: str = "0.50",
    seconds_to_expiry: str = "120",
):
    now = time.time()
    return engine.evaluate(
        slug="btc-updown-5m-1",
        strike=Decimal("100000"),
        spot=Decimal("100000"),
        seconds_to_expiry=Decimal(seconds_to_expiry),
        sigma_per_sqrt_second=Decimal("0.00005"),
        base_probability_up=Decimal(probability_up),
        spot_prices=[Decimal("100000"), Decimal("100000"), Decimal("100000")],
        sample_times=[1.0, 2.0, 3.0],
        up_book=book("up", "0.49", up_ask, observed_at=now),
        down_book=book("down", "0.49", down_ask, observed_at=now),
        observed_at=now,
    )


def test_engine_emits_fee_adjusted_target_inventory_buy() -> None:
    decision = evaluate(FastDirectionalHedgeEngine())

    assert decision is not None
    assert decision.side == "UP"
    assert decision.quantity == Decimal("2.000000")
    assert decision.fair_probability + decision.snapshot.fair_down_probability == Decimal("1")
    assert decision.snapshot.raw_fair_up_probability == Decimal("0.8")
    assert decision.fair_probability == Decimal("0.605")
    assert decision.net_edge > Decimal("0.06")
    assert decision.limit_price == Decimal("0.52")
    assert "exec_vwap=0.5000" in decision.reason
    assert "calibration=0.35" in decision.reason


def test_probability_calibration_is_symmetric_and_monotonic() -> None:
    assert calibrate_probability(Decimal("0.80"), Decimal("0.35")) == Decimal("0.6050")
    assert calibrate_probability(Decimal("0.20"), Decimal("0.35")) == Decimal("0.3950")
    assert calibrate_probability(Decimal("0.50"), Decimal("0.35")) == Decimal("0.5000")
    assert calibrate_probability(Decimal("0.80"), Decimal("1")) == Decimal("0.80")


def test_calibration_cannot_create_underdog_edge_rejected_by_raw_model() -> None:
    engine = FastDirectionalHedgeEngine()
    decision = evaluate(
        engine,
        probability_up="0.80",
        up_ask="0.90",
        down_ask="0.25",
    )

    assert decision is None


def test_engine_rejects_stale_book() -> None:
    engine = FastDirectionalHedgeEngine()
    now = time.time()
    decision = engine.evaluate(
        slug="btc-updown-5m-1",
        strike=Decimal("100000"),
        spot=Decimal("100000"),
        seconds_to_expiry=Decimal("120"),
        sigma_per_sqrt_second=Decimal("0.00005"),
        base_probability_up=Decimal("0.80"),
        spot_prices=[Decimal("100000"), Decimal("100000")],
        sample_times=[1.0, 2.0],
        up_book=book("up", "0.49", "0.50", observed_at=now - 2),
        down_book=book("down", "0.49", "0.50", observed_at=now - 2),
        observed_at=now,
    )
    assert decision is None


def test_pair_conversion_separates_arbitrage_and_hedge_pairs() -> None:
    engine = FastDirectionalHedgeEngine(
        FastDirectionalHedgeSettings(fee_rate=Decimal("0"))
    )
    engine.record_fill("market", "UP", Decimal("2"), Decimal("1.20"))
    engine.record_fill("market", "DOWN", Decimal("1"), Decimal("0.35"))
    metrics = engine.pair_metrics()
    assert metrics.paired_qty == Decimal("1")
    assert metrics.arbitrage_pair_qty == Decimal("1")
    assert metrics.locked_arbitrage_profit == Decimal("0.05")
    assert metrics.directional_side == "UP"
    assert metrics.directional_qty == Decimal("1")

    engine.record_fill("market", "DOWN", Decimal("1"), Decimal("0.50"))
    metrics = engine.pair_metrics()
    assert metrics.arbitrage_pair_qty == Decimal("1")
    assert metrics.hedge_pair_qty == Decimal("1")
    assert metrics.hedge_pair_cost_overrun == Decimal("0.10")
    assert metrics.directional_qty == Decimal("0")


def test_state_restores_fills_and_pair_inventory(tmp_path) -> None:
    state_path = tmp_path / "fdh-state.json"
    events_path = tmp_path / "fdh-events.jsonl"
    settings = FastDirectionalHedgeSettings(fee_rate=Decimal("0"))
    engine = FastDirectionalHedgeEngine(settings, state_path, events_path)
    engine.record_fill("market", "UP", Decimal("2"), Decimal("1.0"), Decimal("0.7"))
    engine.record_fill("market", "DOWN", Decimal("1"), Decimal("0.4"), Decimal("0.3"))

    restored = FastDirectionalHedgeEngine(settings, state_path, events_path)

    assert restored.state.market_slug == "market"
    assert restored.quantities() == (Decimal("2"), Decimal("1"))
    assert restored.state.add_count == 2
    assert restored.pair_metrics().paired_qty == Decimal("1")
    records = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert records[-1]["strategy_version"] == "1.5.0"
    assert records[-1]["model_version"] == "book_lead_v1"


def test_orderbook_first_waits_for_confirmation_then_uses_book_direction() -> None:
    settings = FastDirectionalHedgeSettings(
        orderbook_first=True,
        orderbook_confirmation_seconds=Decimal("0"),
        orderbook_min_imbalance=Decimal("0"),
        orderbook_min_flow_imbalance=Decimal("0"),
        orderbook_max_spread=Decimal("0.20"),
        fee_rate=Decimal("0"),
    )
    engine = FastDirectionalHedgeEngine(settings)

    # The model favors DOWN, but the executable book leads UP at 60%.
    assert evaluate(engine, probability_up="0.20", up_ask="0.60", down_ask="0.40") is None
    decision = evaluate(engine, probability_up="0.20", up_ask="0.60", down_ask="0.40")

    assert decision is not None
    assert decision.side == "UP"
    assert "order_tier=book_entry" in decision.reason


def test_orderbook_first_pairs_immediately_when_held_lead_is_lost() -> None:
    settings = FastDirectionalHedgeSettings(
        orderbook_first=True,
        orderbook_confirmation_seconds=Decimal("0"),
        orderbook_min_imbalance=Decimal("0"),
        orderbook_min_flow_imbalance=Decimal("0"),
        orderbook_max_spread=Decimal("0.20"),
        fee_rate=Decimal("0"),
    )
    engine = FastDirectionalHedgeEngine(settings)
    engine.record_fill("btc-updown-5m-1", "UP", Decimal("2"), Decimal("1.20"))

    decision = evaluate(engine, probability_up="0.90", up_ask="0.49", down_ask="0.51")

    assert decision is not None
    assert decision.side == "DOWN"
    assert decision.quantity == Decimal("2.000000")
    assert "order_tier=book_stop" in decision.reason


def test_risk_limit_stops_adds_after_maximum_count() -> None:
    engine = FastDirectionalHedgeEngine(
        FastDirectionalHedgeSettings(max_add_count=1, fee_rate=Decimal("0"))
    )
    first = evaluate(engine)
    assert first is not None
    engine.record_fill(
        "btc-updown-5m-1",
        first.side,
        first.quantity,
        first.quantity * first.limit_price,
        first.fair_probability,
    )
    assert evaluate(engine) is None


def test_opposite_edge_builds_pair_before_flipping_direction() -> None:
    engine = FastDirectionalHedgeEngine(
        FastDirectionalHedgeSettings(fee_rate=Decimal("0"))
    )
    engine.record_fill("btc-updown-5m-1", "UP", Decimal("4"), Decimal("2"))

    decision = evaluate(
        engine,
        probability_up="0.20",
        up_ask="0.50",
        down_ask="0.50",
    )

    assert decision is not None
    assert decision.side == "DOWN"
    assert decision.quantity == Decimal("4.000000")
    assert "target_ratio=1" in decision.reason


def test_low_probability_cannot_open_directional_inventory() -> None:
    engine = FastDirectionalHedgeEngine(
        FastDirectionalHedgeSettings(fee_rate=Decimal("0"))
    )

    decision = evaluate(
        engine,
        probability_up="0.70",
        up_ask="0.20",
        down_ask="0.90",
    )

    assert decision is None


def test_low_probability_can_complete_only_a_profitable_pair() -> None:
    settings = FastDirectionalHedgeSettings(fee_rate=Decimal("0"))
    profitable = FastDirectionalHedgeEngine(settings)
    profitable.record_fill("btc-updown-5m-1", "UP", Decimal("10"), Decimal("4.00"))

    decision = evaluate(
        profitable,
        probability_up="0.30",
        up_ask="0.90",
        down_ask="0.20",
    )

    assert decision is not None
    assert decision.side == "DOWN"
    assert decision.fair_probability.quantize(Decimal("0.001")) == Decimal("0.570")

    costly = FastDirectionalHedgeEngine(settings)
    costly.record_fill("btc-updown-5m-1", "UP", Decimal("10"), Decimal("9.00"))
    costly_stop = evaluate(
        costly,
        probability_up="0.30",
        up_ask="0.90",
        down_ask="0.20",
    )
    assert costly_stop is not None
    assert costly_stop.quantity == Decimal("10.000000")
    assert "order_tier=edge_stop" in costly_stop.reason


def test_low_probability_pair_can_be_a_bounded_loss_reducing_hedge() -> None:
    settings = FastDirectionalHedgeSettings(fee_rate=Decimal("0"))
    engine = FastDirectionalHedgeEngine(settings)
    engine.record_fill("btc-updown-5m-1", "UP", Decimal("10"), Decimal("5.50"))

    decision = evaluate(
        engine,
        probability_up="0.30",
        up_ask="0.90",
        down_ask="0.50",
        seconds_to_expiry="180",
    )

    assert decision is not None
    assert decision.side == "DOWN"
    assert "order_tier=edge_stop" in decision.reason
    assert decision.quantity * (Decimal("1") - decision.executable_price) >= Decimal("0.25")


def test_late_entry_cutoff_rejects_final_75_seconds() -> None:
    engine = FastDirectionalHedgeEngine()

    assert evaluate(engine, seconds_to_expiry="75") is not None
    assert evaluate(engine, seconds_to_expiry="74.999") is None


def test_orderbook_stop_remains_active_after_opening_cutoff() -> None:
    settings = FastDirectionalHedgeSettings(
        orderbook_first=True,
        fee_rate=Decimal("0"),
    )
    engine = FastDirectionalHedgeEngine(settings)
    engine.record_fill("btc-updown-5m-1", "UP", Decimal("2"), Decimal("1.20"))

    decision = evaluate(
        engine,
        probability_up="0.90",
        up_ask="0.49",
        down_ask="0.51",
        seconds_to_expiry="10",
    )

    assert decision is not None
    assert decision.side == "DOWN"
    assert "order_tier=book_stop" in decision.reason


def test_strong_early_signal_uses_capped_larger_order_tier() -> None:
    engine = FastDirectionalHedgeEngine()

    decision = evaluate(
        engine,
        probability_up="0.90",
        up_ask="0.50",
        down_ask="0.50",
        seconds_to_expiry="180",
    )

    assert decision is not None
    assert decision.side == "UP"
    assert decision.quantity == Decimal("3.000000")
    assert "order_tier=strong" in decision.reason


def test_directional_open_rejects_model_edge_that_disagrees_with_leading_market() -> None:
    engine = FastDirectionalHedgeEngine()

    decision = evaluate(
        engine,
        probability_up="0.90",
        up_ask="0.25",
        down_ask="0.75",
        seconds_to_expiry="180",
    )

    assert decision is None


def test_pair_conversion_is_exempt_from_market_lead_direction_filter() -> None:
    engine = FastDirectionalHedgeEngine(
        FastDirectionalHedgeSettings(fee_rate=Decimal("0"))
    )
    engine.record_fill("btc-updown-5m-1", "UP", Decimal("10"), Decimal("4.00"))

    decision = evaluate(
        engine,
        probability_up="0.30",
        up_ask="0.80",
        down_ask="0.20",
    )

    assert decision is not None
    assert decision.side == "DOWN"


def test_same_side_add_does_not_average_down_against_market_move() -> None:
    engine = FastDirectionalHedgeEngine(
        FastDirectionalHedgeSettings(fee_rate=Decimal("0"))
    )
    engine.record_fill("btc-updown-5m-1", "UP", Decimal("2"), Decimal("1.30"))

    decision = evaluate(
        engine,
        probability_up="0.90",
        up_ask="0.55",
        down_ask="0.45",
        seconds_to_expiry="180",
    )

    assert decision is None


def test_existing_position_is_held_while_its_edge_remains_above_hold_threshold() -> None:
    engine = FastDirectionalHedgeEngine(
        FastDirectionalHedgeSettings(fee_rate=Decimal("0"))
    )
    engine.record_fill("btc-updown-5m-1", "UP", Decimal("2"), Decimal("1.00"))

    decision = evaluate(
        engine,
        probability_up="0.90",
        up_ask="0.50",
        down_ask="0.50",
    )

    assert decision is None


def test_edge_decay_triggers_full_opposite_pair_stop() -> None:
    engine = FastDirectionalHedgeEngine(
        FastDirectionalHedgeSettings(fee_rate=Decimal("0"))
    )
    engine.record_fill("btc-updown-5m-1", "UP", Decimal("4"), Decimal("2.00"))

    decision = evaluate(
        engine,
        probability_up="0.30",
        up_ask="0.40",
        down_ask="0.60",
    )

    assert decision is not None
    assert decision.side == "DOWN"
    assert decision.quantity == Decimal("4.000000")
    assert "order_tier=edge_stop" in decision.reason
