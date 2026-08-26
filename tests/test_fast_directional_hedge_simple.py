from __future__ import annotations

from decimal import Decimal

from src.fast_directional_hedge_simple import (
    FastDirectionalHedgeSimpleEngine,
    FastDirectionalHedgeSimpleSettings,
)
from src.polymarket import OrderBookLevel, OrderBookSnapshot


def book(token: str, bid: str, ask: str, timestamp: float, size: str = "20") -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token,
        timestamp=str(int(timestamp * 1000)),
        bids=(OrderBookLevel(Decimal(bid), Decimal(size)),),
        asks=(OrderBookLevel(Decimal(ask), Decimal(size)),),
        minimum_order_size=Decimal("1"),
    )


def evaluate(
    engine: FastDirectionalHedgeSimpleEngine,
    timestamp: float,
    *,
    up_bid: str = "0.53",
    up_ask: str = "0.54",
    down_bid: str = "0.45",
    down_ask: str = "0.46",
    seconds: str = "120",
    spot: str = "125",
    price_to_beat: str = "100",
    sigma: str = "1",
):
    return engine.evaluate(
        slug="btc-updown-5m-1",
        seconds_to_expiry=Decimal(seconds),
        up_book=book("up", up_bid, up_ask, timestamp),
        down_book=book("down", down_bid, down_ask, timestamp),
        spot_price=Decimal(spot),
        price_to_beat=Decimal(price_to_beat),
        sigma_per_sqrt_second=Decimal(sigma),
        observed_at=timestamp,
    )


def open_up(engine: FastDirectionalHedgeSimpleEngine, timestamp: float = 1000.0) -> None:
    assert evaluate(engine, timestamp) is None
    decision = evaluate(engine, timestamp + 0.2)
    assert decision is not None and decision.role == "ENTRY" and decision.side == "UP"
    engine.record_fill("btc-updown-5m-1", "UP", Decimal("2"), Decimal("1.10"))


def test_two_distinct_book_ticks_open_after_minimum_interval() -> None:
    engine = FastDirectionalHedgeSimpleEngine()

    assert evaluate(engine, 1000.0) is None
    assert evaluate(engine, 1000.01) is None
    decision = evaluate(engine, 1000.2)

    assert decision is not None
    assert decision.side == "UP"
    assert decision.quantity == Decimal("2.000000")
    assert decision.limit_price <= Decimal("0.56")
    assert "role=ENTRY" in decision.reason


def test_entry_drift_cancels_the_pending_signal() -> None:
    engine = FastDirectionalHedgeSimpleEngine()

    assert evaluate(engine, 1000.0, up_ask="0.54") is None
    assert evaluate(engine, 1000.2, up_ask="0.57", down_ask="0.43") is None
    assert engine.state.candidate_side is None


def test_normal_initial_stop_requires_two_ticks_then_hedges() -> None:
    engine = FastDirectionalHedgeSimpleEngine()
    open_up(engine)

    assert evaluate(engine, 1001.0, up_bid="0.40", up_ask="0.41", down_bid="0.58", down_ask="0.59", spot="95") is None
    hedge = evaluate(engine, 1001.1, up_bid="0.40", up_ask="0.41", down_bid="0.58", down_ask="0.59", spot="95")

    assert hedge is not None
    assert hedge.role == "HEDGE"
    assert hedge.side == "DOWN"
    assert "speed=NORMAL" in hedge.reason


def test_fast_stop_and_emergency_stop_need_only_one_tick() -> None:
    fast = FastDirectionalHedgeSimpleEngine()
    open_up(fast)
    assert evaluate(fast, 1001.0, up_bid="0.55", up_ask="0.56", down_bid="0.43", down_ask="0.44") is None
    fast_hedge = evaluate(fast, 1001.5, up_bid="0.40", up_ask="0.41", down_bid="0.58", down_ask="0.59", spot="95")
    assert fast_hedge is not None
    assert "speed=FAST" in fast_hedge.reason

    emergency = FastDirectionalHedgeSimpleEngine()
    open_up(emergency, 2000.0)
    emergency_hedge = evaluate(
        emergency,
        2001.0,
        up_bid="0.34",
        up_ask="0.35",
        down_bid="0.64",
        down_ask="0.65",
    )
    assert emergency_hedge is not None
    assert "speed=EMERGENCY" in emergency_hedge.reason


def test_trailing_stop_starts_at_gain_and_never_moves_down() -> None:
    engine = FastDirectionalHedgeSimpleEngine(
        FastDirectionalHedgeSimpleSettings(take_profit_net_per_share=Decimal("0.50"))
    )
    open_up(engine)
    trade = engine.state.active_trade
    assert trade is not None

    assert evaluate(engine, 1001.0, up_bid="0.71", up_ask="0.72", down_bid="0.27", down_ask="0.28") is None
    first_stop = trade.final_stop_price
    assert first_stop >= trade.entry_price
    assert evaluate(engine, 1002.0, up_bid="0.80", up_ask="0.81", down_bid="0.18", down_ask="0.19") is None
    raised_stop = trade.final_stop_price
    assert raised_stop > first_stop
    assert evaluate(engine, 1003.0, up_bid="0.75", up_ask="0.76", down_bid="0.23", down_ask="0.24") is None
    assert trade.final_stop_price == raised_stop


def test_partial_hedge_keeps_retrying_only_the_remaining_exposure() -> None:
    engine = FastDirectionalHedgeSimpleEngine()
    open_up(engine)
    assert evaluate(engine, 1001.0, up_bid="0.34", up_ask="0.35", down_bid="0.64", down_ask="0.65") is not None

    engine.record_fill("btc-updown-5m-1", "DOWN", Decimal("1"), Decimal("0.61"))
    retry = evaluate(engine, 1001.1, up_bid="0.33", up_ask="0.34", down_bid="0.65", down_ask="0.66")
    assert retry is not None
    assert retry.quantity == Decimal("1.000000")

    engine.record_fill("btc-updown-5m-1", "DOWN", Decimal("1"), Decimal("0.62"))
    assert engine.state.active_trade is None
    assert engine.state.completed_trades[-1].status == "HEDGED"


def test_state_restores_directional_position_and_stop(tmp_path) -> None:
    path = tmp_path / "simple-state.json"
    engine = FastDirectionalHedgeSimpleEngine(
        FastDirectionalHedgeSimpleSettings(take_profit_net_per_share=Decimal("0.50")),
        state_path=path,
    )
    open_up(engine)
    assert evaluate(engine, 1001.0, up_bid="0.71", up_ask="0.72", down_bid="0.27", down_ask="0.28") is None

    restored = FastDirectionalHedgeSimpleEngine(
        FastDirectionalHedgeSimpleSettings(take_profit_net_per_share=Decimal("0.50")),
        state_path=path,
    )
    assert restored.quantities() == (Decimal("2"), Decimal("0"))
    assert restored.state.active_trade is not None
    assert restored.state.active_trade.final_stop_price >= Decimal("0.55")


def test_uncertain_submission_freezes_new_orders_for_reconciliation() -> None:
    engine = FastDirectionalHedgeSimpleEngine()
    assert evaluate(engine, 1000.0) is None
    assert evaluate(engine, 1000.2) is not None
    engine.mark_submission_started("ENTRY")
    engine.mark_submission_failed(uncertain=True)

    assert evaluate(engine, 1000.2) is None
    assert engine.state.active_trade is not None
    assert engine.state.active_trade.submission_uncertain is True


def test_startup_reconciliation_recovers_an_uncertain_partial_hedge() -> None:
    engine = FastDirectionalHedgeSimpleEngine()
    open_up(engine)
    assert evaluate(engine, 1001.0, up_bid="0.34", up_ask="0.35", down_bid="0.64", down_ask="0.65") is not None
    engine.mark_submission_started("HEDGE")
    engine.mark_submission_failed(uncertain=True)

    engine.reconcile_positions(Decimal("2"), Decimal("1"))

    assert engine.state.active_trade is not None
    assert engine.state.active_trade.status == "RISK_EXIT"
    assert engine.state.active_trade.remaining_exposure == Decimal("1")
    assert engine.state.active_trade.submission_uncertain is False


def test_reconciliation_rejects_unexplained_wallet_inventory() -> None:
    engine = FastDirectionalHedgeSimpleEngine()

    try:
        engine.reconcile_positions(Decimal("2"), Decimal("0"))
    except RuntimeError as exc:
        assert "absent from local strategy state" in str(exc)
    else:
        raise AssertionError("unexplained inventory must block startup")


def test_settlement_records_stop_recovery_and_pnl_metrics() -> None:
    engine = FastDirectionalHedgeSimpleEngine()
    open_up(engine)
    assert evaluate(engine, 1001.0, up_bid="0.34", up_ask="0.35", down_bid="0.64", down_ask="0.65") is not None
    engine.record_fill("btc-updown-5m-1", "DOWN", Decimal("2"), Decimal("1.22"))

    engine.record_settlement("btc-updown-5m-1", "UP")
    summary = engine.execution_summary()

    assert summary["stop_recovery_rate"] == 1.0
    assert summary["hedge_locked_loss_count"] == 1
    assert summary["fees"] > 0
    assert summary["net_pnl"] < 0


def test_entry_requires_time_ask_gap_and_spread_but_not_btc_confirmation() -> None:
    engine = FastDirectionalHedgeSimpleEngine()
    assert evaluate(engine, 1000.0, seconds="200") is None
    assert evaluate(engine, 1000.2, seconds="200") is None

    engine = FastDirectionalHedgeSimpleEngine()
    assert evaluate(engine, 1000.0, spot="110") is None
    assert evaluate(engine, 1000.2, spot="110") is not None

    engine = FastDirectionalHedgeSimpleEngine()
    assert evaluate(engine, 1000.0, spot="0", price_to_beat="0") is None
    assert evaluate(engine, 1000.2, spot="0", price_to_beat="0") is not None

    engine = FastDirectionalHedgeSimpleEngine()
    assert evaluate(engine, 1000.0, up_ask="0.54", down_ask="0.51") is None
    assert evaluate(engine, 1000.2, up_ask="0.54", down_ask="0.51") is None

    engine = FastDirectionalHedgeSimpleEngine()
    assert evaluate(engine, 1000.0, up_bid="0.48", up_ask="0.54") is None
    assert evaluate(engine, 1000.2, up_bid="0.48", up_ask="0.54") is None


def test_normal_stop_uses_executable_book_without_btc_confirmation() -> None:
    engine = FastDirectionalHedgeSimpleEngine()
    open_up(engine)

    assert evaluate(engine, 1001.0, up_bid="0.40", up_ask="0.41", down_bid="0.58", down_ask="0.59") is None
    decision = evaluate(
        engine,
        1001.2,
        up_bid="0.40",
        up_ask="0.41",
        down_bid="0.58",
        down_ask="0.59",
    )
    assert decision is not None
    assert decision.role == "HEDGE"


def test_executable_net_profit_is_sold_as_volatility_arbitrage() -> None:
    engine = FastDirectionalHedgeSimpleEngine(
        FastDirectionalHedgeSimpleSettings(take_profit_net_per_share=Decimal("0.02"))
    )
    open_up(engine)

    decision = evaluate(
        engine,
        1001.0,
        up_bid="0.62",
        up_ask="0.63",
        down_bid="0.36",
        down_ask="0.37",
    )

    assert decision is not None
    assert decision.role == "EXIT"
    assert decision.action == "SELL"
    assert decision.limit_price == Decimal("0.62")
    assert "exit_type=TAKE_PROFIT" in decision.reason
    engine.record_exit_fill("btc-updown-5m-1", "UP", Decimal("2"), Decimal("1.24"))
    assert engine.state.completed_trades[-1].result == "VOLATILITY_ARBITRAGE_PROFIT"


def test_unfilled_take_profit_does_not_chase_after_profit_disappears() -> None:
    engine = FastDirectionalHedgeSimpleEngine()
    open_up(engine)
    decision = evaluate(
        engine,
        1001.0,
        up_bid="0.62",
        up_ask="0.63",
        down_bid="0.36",
        down_ask="0.37",
    )
    assert decision is not None and decision.role == "EXIT"
    engine.mark_submission_started("EXIT")
    engine.mark_submission_failed(uncertain=False)

    assert evaluate(
        engine,
        1001.2,
        up_bid="0.58",
        up_ask="0.59",
        down_bid="0.40",
        down_ask="0.41",
    ) is None
    assert engine.state.active_trade is not None
    assert engine.state.active_trade.status == "DIRECTIONAL"
    assert engine.state.active_trade.risk_exit_mode is None


def test_risk_exit_sells_original_when_net_value_beats_hedge() -> None:
    engine = FastDirectionalHedgeSimpleEngine()
    open_up(engine)

    assert evaluate(
        engine,
        1001.0,
        up_bid="0.40",
        up_ask="0.41",
        down_bid="0.68",
        down_ask="0.70",
        spot="95",
    ) is None
    decision = evaluate(
        engine,
        1001.2,
        up_bid="0.40",
        up_ask="0.41",
        down_bid="0.68",
        down_ask="0.70",
        spot="95",
    )

    assert decision is not None
    assert decision.role == "EXIT"
    assert decision.action == "SELL"
    assert decision.side == "UP"

    engine.record_exit_fill("btc-updown-5m-1", "UP", Decimal("2"), Decimal("0.80"))
    engine.record_settlement("btc-updown-5m-1", "UP")
    summary = engine.execution_summary()
    assert summary["stop_exit_count"] == 1
    assert summary["stop_recovery_rate"] == 1.0


def test_completed_risk_exit_does_not_reenter_same_window() -> None:
    engine = FastDirectionalHedgeSimpleEngine(
        FastDirectionalHedgeSimpleSettings(max_entries_per_window=1)
    )
    open_up(engine)
    assert evaluate(
        engine,
        1001.0,
        up_bid="0.34",
        up_ask="0.35",
        down_bid="0.64",
        down_ask="0.65",
    ) is not None
    engine.record_fill("btc-updown-5m-1", "DOWN", Decimal("2"), Decimal("1.30"))

    assert evaluate(engine, 1002.0) is None
    assert evaluate(engine, 1002.2) is None
