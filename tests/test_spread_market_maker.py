from decimal import Decimal

from src.spread_market_maker import (
    ActionKind,
    BookSide,
    InventoryPosition,
    MakerState,
    OrderSide,
    OutcomeSide,
    SpreadMarketMaker,
    SpreadSnapshot,
    WorkingOrder,
)


def snap(**overrides) -> SpreadSnapshot:
    values = {
        "seconds_left": 200,
        "observed_at": Decimal("100"),
        "quote_age_seconds": Decimal("0.5"),
        "up": BookSide(Decimal("0.48"), Decimal("0.52"), Decimal("10")),
        "down": BookSide(Decimal("0.49"), Decimal("0.53"), Decimal("10")),
        "absolute_window_move": Decimal("0.0005"),
        "short_volatility": Decimal("0.0004"),
    }
    values.update(overrides)
    return SpreadSnapshot(**values)


def test_places_paired_post_only_buys() -> None:
    actions = SpreadMarketMaker().plan(snap(), MakerState())
    assert len(actions) == 2
    assert {item.outcome for item in actions} == {OutcomeSide.UP, OutcomeSide.DOWN}
    assert all(item.kind is ActionKind.PLACE_POST_ONLY for item in actions)
    assert all(item.order_side is OrderSide.BUY for item in actions)


def test_rejects_pair_without_locked_margin() -> None:
    market = snap(
        up=BookSide(Decimal("0.50"), Decimal("0.54"), Decimal("10")),
        down=BookSide(Decimal("0.50"), Decimal("0.54"), Decimal("10")),
    )
    assert SpreadMarketMaker().plan(market, MakerState()) == ()


def test_inventory_gets_post_only_take_profit() -> None:
    state = MakerState(inventory=(InventoryPosition(
        OutcomeSide.UP, Decimal("2"), Decimal("0.45"), Decimal("95")
    ),))
    actions = SpreadMarketMaker().plan(snap(), state)
    assert len(actions) == 1
    assert actions[0].order_side is OrderSide.SELL
    assert actions[0].kind is ActionKind.PLACE_POST_ONLY
    assert actions[0].price == Decimal("0.52")


def test_stop_loss_cancels_sell_then_uses_fak() -> None:
    state = MakerState(
        inventory=(InventoryPosition(
            OutcomeSide.UP, Decimal("2"), Decimal("0.55"), Decimal("95")
        ),),
        working_orders=(WorkingOrder(
            "sell-1", OutcomeSide.UP, OrderSide.SELL,
            Decimal("0.58"), Decimal("2"), Decimal("96")
        ),),
    )
    market = snap(up=BookSide(Decimal("0.49"), Decimal("0.53"), Decimal("10")))
    actions = SpreadMarketMaker().plan(market, state)
    assert [item.kind for item in actions] == [ActionKind.CANCEL, ActionKind.SELL_FAK]
    assert actions[-1].price == Decimal("0.49")


def test_unfilled_pair_leg_is_cancelled_after_timeout() -> None:
    state = MakerState(
        inventory=(InventoryPosition(
            OutcomeSide.UP, Decimal("2"), Decimal("0.48"), Decimal("90")
        ),),
        working_orders=(WorkingOrder(
            "down-buy", OutcomeSide.DOWN, OrderSide.BUY,
            Decimal("0.49"), Decimal("2"), Decimal("89")
        ),),
    )
    actions = SpreadMarketMaker().plan(snap(), state)
    assert actions[0].kind is ActionKind.CANCEL
    assert actions[0].order_id == "down-buy"
    assert any(item.order_side is OrderSide.SELL for item in actions)


def test_force_exit_cancels_and_flattens() -> None:
    state = MakerState(
        inventory=(InventoryPosition(
            OutcomeSide.DOWN, Decimal("2"), Decimal("0.49"), Decimal("95")
        ),),
        working_orders=(WorkingOrder(
            "sell-1", OutcomeSide.DOWN, OrderSide.SELL,
            Decimal("0.52"), Decimal("2"), Decimal("96")
        ),),
    )
    actions = SpreadMarketMaker().plan(snap(seconds_left=30), state)
    assert [item.kind for item in actions] == [ActionKind.CANCEL, ActionKind.SELL_FAK]


def test_unhealthy_book_cancels_entry_quote() -> None:
    state = MakerState(working_orders=(WorkingOrder(
        "buy-1", OutcomeSide.UP, OrderSide.BUY,
        Decimal("0.48"), Decimal("2"), Decimal("99")
    ),))
    market = snap(up=BookSide(Decimal("0.50"), Decimal("0.51"), Decimal("10")))
    actions = SpreadMarketMaker().plan(market, state)
    assert len(actions) == 1
    assert actions[0].kind is ActionKind.CANCEL
