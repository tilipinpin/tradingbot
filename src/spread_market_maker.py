from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from enum import Enum


class OutcomeSide(str, Enum):
    UP = "UP"
    DOWN = "DOWN"

    @property
    def opposite(self) -> "OutcomeSide":
        return OutcomeSide.DOWN if self is OutcomeSide.UP else OutcomeSide.UP


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ActionKind(str, Enum):
    PLACE_POST_ONLY = "PLACE_POST_ONLY"
    CANCEL = "CANCEL"
    SELL_FAK = "SELL_FAK"


@dataclass(frozen=True)
class SpreadMakerSettings:
    entry_start_seconds: int = 270
    entry_cutoff_seconds: int = 90
    force_exit_seconds: int = 30
    quote_ttl_seconds: int = 8
    leg_timeout_seconds: int = 4
    inventory_timeout_seconds: int = 20
    order_size: Decimal = Decimal("5")
    max_window_notional: Decimal = Decimal("5.00")
    minimum_spread: Decimal = Decimal("0.03")
    minimum_pair_margin: Decimal = Decimal("0.01")
    target_profit: Decimal = Decimal("0.03")
    stop_loss: Decimal = Decimal("0.05")
    minimum_price: Decimal = Decimal("0.20")
    maximum_price: Decimal = Decimal("0.80")
    minimum_bid_depth: Decimal = Decimal("4")
    maximum_quote_age_seconds: Decimal = Decimal("2")
    maximum_window_move: Decimal = Decimal("0.0015")
    maximum_short_volatility: Decimal = Decimal("0.0010")

    def __post_init__(self) -> None:
        if not 300 >= self.entry_start_seconds > self.entry_cutoff_seconds > self.force_exit_seconds >= 0:
            raise ValueError("invalid spread-maker time window")
        if min(self.quote_ttl_seconds, self.leg_timeout_seconds, self.inventory_timeout_seconds) <= 0:
            raise ValueError("spread-maker timeouts must be positive")
        if min(self.order_size, self.max_window_notional, self.minimum_spread, self.target_profit, self.stop_loss) <= 0:
            raise ValueError("spread-maker sizes and thresholds must be positive")
        if not Decimal("0") < self.minimum_pair_margin < Decimal("1"):
            raise ValueError("minimum_pair_margin must be between zero and one")
        if not Decimal("0") < self.minimum_price < self.maximum_price < Decimal("1"):
            raise ValueError("invalid spread-maker price range")


@dataclass(frozen=True)
class BookSide:
    bid: Decimal | None
    ask: Decimal | None
    bid_depth: Decimal = Decimal("0")

    @property
    def spread(self) -> Decimal | None:
        return None if self.bid is None or self.ask is None else self.ask - self.bid


@dataclass(frozen=True)
class SpreadSnapshot:
    seconds_left: int
    observed_at: Decimal
    quote_age_seconds: Decimal
    up: BookSide
    down: BookSide
    absolute_window_move: Decimal
    short_volatility: Decimal
    tick_size: Decimal = Decimal("0.01")


@dataclass(frozen=True)
class InventoryPosition:
    side: OutcomeSide
    shares: Decimal
    average_price: Decimal
    first_fill_at: Decimal


@dataclass(frozen=True)
class WorkingOrder:
    order_id: str
    outcome: OutcomeSide
    order_side: OrderSide
    price: Decimal
    shares: Decimal
    created_at: Decimal


@dataclass(frozen=True)
class MakerState:
    inventory: tuple[InventoryPosition, ...] = ()
    working_orders: tuple[WorkingOrder, ...] = ()
    committed_notional: Decimal = Decimal("0")


@dataclass(frozen=True)
class MakerAction:
    kind: ActionKind
    outcome: OutcomeSide
    order_side: OrderSide | None = None
    price: Decimal | None = None
    shares: Decimal | None = None
    order_id: str | None = None
    reason: str = ""


class SpreadMarketMaker:
    """Pure paired maker decision engine; it never submits orders itself."""

    def __init__(self, settings: SpreadMakerSettings | None = None) -> None:
        self.settings = settings or SpreadMakerSettings()

    def plan(self, snapshot: SpreadSnapshot, state: MakerState) -> tuple[MakerAction, ...]:
        if snapshot.tick_size <= 0:
            raise ValueError("tick_size must be positive")
        if snapshot.seconds_left <= self.settings.force_exit_seconds:
            return self._force_exit(snapshot, state)
        if state.inventory:
            return self._manage_inventory(snapshot, state)
        if snapshot.seconds_left <= self.settings.entry_cutoff_seconds:
            return self._cancel_buys(state, "entry cutoff reached")
        if snapshot.seconds_left > self.settings.entry_start_seconds:
            return self._cancel_buys(state, "waiting for opening volatility")
        unhealthy = self._unhealthy_reason(snapshot)
        if unhealthy:
            return self._cancel_buys(state, unhealthy)
        stale = tuple(
            order for order in state.working_orders
            if order.order_side is OrderSide.BUY
            and snapshot.observed_at - order.created_at >= Decimal(self.settings.quote_ttl_seconds)
        )
        if stale:
            return tuple(self._cancel(order, "maker quote expired") for order in stale)
        if any(order.order_side is OrderSide.BUY for order in state.working_orders):
            return ()
        if state.committed_notional > 0:
            return ()
        up_price, down_price = snapshot.up.bid, snapshot.down.bid
        assert up_price is not None and down_price is not None
        pair_cost = up_price + down_price
        if pair_cost > Decimal("1") - self.settings.minimum_pair_margin:
            return ()
        if self.settings.order_size * pair_cost > self.settings.max_window_notional:
            return ()
        return (
            self._post_buy(OutcomeSide.UP, up_price),
            self._post_buy(OutcomeSide.DOWN, down_price),
        )

    def _manage_inventory(self, snapshot: SpreadSnapshot, state: MakerState) -> tuple[MakerAction, ...]:
        actions: list[MakerAction] = []
        positions = {item.side: item for item in state.inventory}
        for order in state.working_orders:
            if order.order_side is not OrderSide.BUY:
                continue
            same_filled = order.outcome in positions
            opposite = positions.get(order.outcome.opposite)
            leg_expired = opposite is not None and snapshot.observed_at - opposite.first_fill_at >= Decimal(self.settings.leg_timeout_seconds)
            if same_filled or leg_expired:
                actions.append(self._cancel(order, "paired-leg exposure limit"))
        for position in state.inventory:
            book = snapshot.up if position.side is OutcomeSide.UP else snapshot.down
            if book.bid is None:
                continue
            age = snapshot.observed_at - position.first_fill_at
            adverse = book.bid <= position.average_price - self.settings.stop_loss
            expired = age >= Decimal(self.settings.inventory_timeout_seconds)
            if adverse or expired or snapshot.seconds_left <= self.settings.entry_cutoff_seconds:
                actions.extend(self._cancel_side(state, position.side, "inventory exit"))
                actions.append(MakerAction(
                    ActionKind.SELL_FAK, position.side, OrderSide.SELL,
                    book.bid, position.shares,
                    reason="inventory stop-loss" if adverse else "inventory timeout/cutoff",
                ))
                continue
            if any(order.outcome is position.side and order.order_side is OrderSide.SELL for order in state.working_orders):
                continue
            target = self._round_up(position.average_price + self.settings.target_profit, snapshot.tick_size)
            if book.ask is not None:
                target = max(target, book.ask)
            if target < Decimal("1"):
                actions.append(MakerAction(
                    ActionKind.PLACE_POST_ONLY, position.side, OrderSide.SELL,
                    target, position.shares, reason="maker take-profit",
                ))
        return tuple(actions)

    def _force_exit(self, snapshot: SpreadSnapshot, state: MakerState) -> tuple[MakerAction, ...]:
        actions = [self._cancel(order, "window force-exit") for order in state.working_orders]
        for position in state.inventory:
            book = snapshot.up if position.side is OutcomeSide.UP else snapshot.down
            if book.bid is not None:
                actions.append(MakerAction(
                    ActionKind.SELL_FAK, position.side, OrderSide.SELL,
                    book.bid, position.shares, reason="window force-exit",
                ))
        return tuple(actions)

    def _unhealthy_reason(self, snapshot: SpreadSnapshot) -> str | None:
        if snapshot.quote_age_seconds > self.settings.maximum_quote_age_seconds:
            return "stale order book"
        if snapshot.absolute_window_move > self.settings.maximum_window_move:
            return "extreme window move"
        if snapshot.short_volatility > self.settings.maximum_short_volatility:
            return "short volatility too high"
        for side, book in ((OutcomeSide.UP, snapshot.up), (OutcomeSide.DOWN, snapshot.down)):
            if book.bid is None or book.ask is None or book.spread is None:
                return f"{side.value} book incomplete"
            if not self.settings.minimum_price <= book.bid <= self.settings.maximum_price:
                return f"{side.value} bid outside maker range"
            if book.spread < self.settings.minimum_spread:
                return f"{side.value} spread too narrow"
            if book.bid_depth < self.settings.minimum_bid_depth:
                return f"{side.value} bid depth insufficient"
        return None

    def _cancel_buys(self, state: MakerState, reason: str) -> tuple[MakerAction, ...]:
        return tuple(self._cancel(order, reason) for order in state.working_orders if order.order_side is OrderSide.BUY)

    def _cancel_side(self, state: MakerState, side: OutcomeSide, reason: str) -> list[MakerAction]:
        return [self._cancel(order, reason) for order in state.working_orders if order.outcome is side]

    def _post_buy(self, side: OutcomeSide, price: Decimal) -> MakerAction:
        return MakerAction(ActionKind.PLACE_POST_ONLY, side, OrderSide.BUY, price, self.settings.order_size, reason="paired maker entry")

    @staticmethod
    def _cancel(order: WorkingOrder, reason: str) -> MakerAction:
        return MakerAction(ActionKind.CANCEL, order.outcome, order.order_side, order_id=order.order_id, reason=reason)

    @staticmethod
    def _round_up(value: Decimal, tick_size: Decimal) -> Decimal:
        return (value / tick_size).to_integral_value(rounding=ROUND_CEILING) * tick_size
