from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.polymarket import ClobTradingClient, Market
from src.spread_market_maker import (
    ActionKind,
    InventoryPosition,
    MakerAction,
    MakerState,
    OrderSide,
    OutcomeSide,
    SpreadMarketMaker,
    SpreadSnapshot,
    WorkingOrder,
)


TERMINAL_ORDER_STATUSES = {"cancelled", "canceled", "matched", "filled", "expired"}


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    return Decimal(str(value))


def _order_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("orderID") or payload.get("order_id") or payload.get("id") or "").strip()


@dataclass
class PersistedOrder:
    order_id: str
    outcome: str
    order_side: str
    price: str
    shares: str
    created_at: str


@dataclass
class SpreadRuntimeState:
    window_slug: str | None = None
    token_ids: list[str] = field(default_factory=list)
    orders: list[PersistedOrder] = field(default_factory=list)
    balances: dict[str, str] = field(default_factory=lambda: {"UP": "0", "DOWN": "0"})
    average_prices: dict[str, str] = field(default_factory=lambda: {"UP": "0", "DOWN": "0"})
    first_fill_at: dict[str, str] = field(default_factory=dict)
    committed_notional: str = "0"
    realized_pnl: str = "0"
    paused_window: str | None = None
    pause_reason: str | None = None
    submission_uncertain: bool = False


@dataclass(frozen=True)
class SpreadTickResult:
    status: str
    actions: tuple[MakerAction, ...] = ()
    responses: tuple[dict[str, Any], ...] = ()
    detail: str = ""


class SpreadMakerRuntime:
    """Crash-safe execution adapter for the pure spread-maker planner."""

    def __init__(
        self,
        strategy: SpreadMarketMaker,
        trader: ClobTradingClient,
        state_path: Path,
        signature_type: int,
        live: bool,
    ) -> None:
        self.strategy = strategy
        self.trader = trader
        self.state_path = state_path
        self.signature_type = signature_type
        self.live = live
        self.state = self._load()

    def _load(self) -> SpreadRuntimeState:
        if not self.state_path.exists():
            return SpreadRuntimeState()
        payload = json.loads(self.state_path.read_text())
        payload["orders"] = [PersistedOrder(**item) for item in payload.get("orders", [])]
        return SpreadRuntimeState(**payload)

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=2) + "\n")
        temp.replace(self.state_path)

    def startup_self_check(self, market: Market) -> dict[str, Any]:
        open_orders = self.trader.open_orders()
        known = {item.order_id for item in self.state.orders}
        unknown = [item for item in open_orders if _order_id(item) not in known]
        if unknown:
            raise RuntimeError(f"spread maker found {len(unknown)} unknown open order(s)")
        balances = self._balances(market)
        if self.state.window_slug not in (None, market.slug) and any(value > Decimal("0.0001") for value in balances.values()):
            raise RuntimeError("spread maker has unresolved inventory from a previous window")
        if self.state.window_slug is None and any(value > Decimal("0.0001") for value in balances.values()):
            raise RuntimeError("spread maker found untracked current-window inventory")
        if self.state.window_slug != market.slug:
            self._select_window(market)
        self._apply_balances(balances, Decimal(str(time.time())))
        self.save()
        return {
            "open_orders": len(open_orders),
            "up_balance": str(balances[OutcomeSide.UP]),
            "down_balance": str(balances[OutcomeSide.DOWN]),
            "collateral": str(self.trader.collateral_balance(self.signature_type)),
        }

    def flat(self) -> bool:
        return not self.state.orders and all(_decimal(value) <= Decimal("0.0001") for value in self.state.balances.values())

    def tick(self, market: Market, snapshot: SpreadSnapshot) -> SpreadTickResult:
        if self.state.submission_uncertain:
            return SpreadTickResult("paused", detail="uncertain prior submission requires manual reconciliation")
        if self.state.window_slug != market.slug:
            if not self.flat():
                self._pause(market.slug, "previous-window orders or inventory remain")
                return SpreadTickResult("paused", detail=self.state.pause_reason or "")
            self._select_window(market)
        if self.state.paused_window == market.slug:
            return SpreadTickResult("paused", detail=self.state.pause_reason or "window paused")

        self._apply_balances(self._balances(market), snapshot.observed_at)
        self._reconcile_orders()
        maker_state = self._maker_state()
        actions = self.strategy.plan(snapshot, maker_state)
        if not actions:
            self.save()
            return SpreadTickResult("idle")

        responses: list[dict[str, Any]] = []
        cancels = [item for item in actions if item.kind is ActionKind.CANCEL]
        placements = [item for item in actions if item.kind is not ActionKind.CANCEL]
        if cancels:
            ids = [item.order_id for item in cancels if item.order_id]
            if self.live:
                self.trader.cancel_orders(ids)
            self.state.orders = [item for item in self.state.orders if item.order_id not in ids]
            self.save()
            # Never cancel and replace from the same stale snapshot.
            return SpreadTickResult("cancelled", tuple(cancels), detail=", ".join(item.reason for item in cancels))

        try:
            for action in placements:
                response = self._submit(market, action)
                responses.append(response)
                if action.kind is ActionKind.PLACE_POST_ONLY:
                    identifier = _order_id(response)
                    if self.live and not identifier:
                        raise RuntimeError("post-only submission returned no order id")
                    if identifier:
                        self.state.orders.append(PersistedOrder(
                            identifier,
                            action.outcome.value,
                            action.order_side.value if action.order_side else "",
                            str(action.price),
                            str(action.shares),
                            str(snapshot.observed_at),
                        ))
                        self.save()
            if any(action.order_side is OrderSide.BUY for action in placements):
                self.state.committed_notional = str(sum(
                    (action.price or Decimal("0")) * (action.shares or Decimal("0"))
                    for action in placements if action.order_side is OrderSide.BUY
                ))
            self.save()
            return SpreadTickResult("submitted", tuple(placements), tuple(responses))
        except Exception as exc:
            # A known first leg is cancelled immediately if the paired post fails.
            known_ids = [_order_id(item) for item in responses if _order_id(item)]
            if known_ids:
                try:
                    if self.live:
                        self.trader.cancel_orders(known_ids)
                    self.state.orders = [item for item in self.state.orders if item.order_id not in known_ids]
                except Exception:
                    self.state.submission_uncertain = True
            else:
                self.state.submission_uncertain = True
            self._pause(market.slug, f"submission failure: {type(exc).__name__}: {exc}")
            raise

    def _submit(self, market: Market, action: MakerAction) -> dict[str, Any]:
        if action.price is None or action.shares is None or action.order_side is None:
            raise ValueError("incomplete spread-maker order action")
        if not self.live:
            return {"success": True, "status": "live", "orderID": f"paper-{time.time_ns()}"}
        token = market.token_ids[0 if action.outcome is OutcomeSide.UP else 1]
        kwargs = dict(
            token_id=token,
            price=action.price,
            size=action.shares,
            tick_size=market.minimum_tick_size,
            neg_risk=market.neg_risk,
        )
        if action.order_side is OrderSide.BUY:
            return self.trader.buy_limit(
                **kwargs,
                order_type="GTC",
                post_only=True,
            )
        return self.trader.sell_limit(
            **kwargs,
            order_type="GTC" if action.kind is ActionKind.PLACE_POST_ONLY else "FAK",
            post_only=action.kind is ActionKind.PLACE_POST_ONLY,
        )

    def _select_window(self, market: Market) -> None:
        self.state = SpreadRuntimeState(window_slug=market.slug, token_ids=list(market.token_ids))
        self.save()

    def _balances(self, market: Market) -> dict[OutcomeSide, Decimal]:
        return {
            OutcomeSide.UP: self.trader.conditional_balance(market.token_ids[0], self.signature_type),
            OutcomeSide.DOWN: self.trader.conditional_balance(market.token_ids[1], self.signature_type),
        }

    def _apply_balances(self, balances: dict[OutcomeSide, Decimal], observed_at: Decimal) -> None:
        orders = {(OutcomeSide(item.outcome), OrderSide(item.order_side)): item for item in self.state.orders}
        for side in OutcomeSide:
            old = _decimal(self.state.balances.get(side.value))
            new = balances[side]
            if new > old:
                added = new - old
                source = orders.get((side, OrderSide.BUY))
                price = _decimal(source.price) if source else Decimal("0")
                old_avg = _decimal(self.state.average_prices.get(side.value))
                self.state.average_prices[side.value] = str(
                    ((old * old_avg) + (added * price)) / new if new else Decimal("0")
                )
                self.state.first_fill_at.setdefault(side.value, str(observed_at))
            elif new < old:
                sold = old - new
                source = orders.get((side, OrderSide.SELL))
                sell_price = _decimal(source.price) if source else Decimal("0")
                cost = _decimal(self.state.average_prices.get(side.value))
                self.state.realized_pnl = str(_decimal(self.state.realized_pnl) + sold * (sell_price - cost))
                if new <= Decimal("0.0001"):
                    self.state.average_prices[side.value] = "0"
                    self.state.first_fill_at.pop(side.value, None)
            self.state.balances[side.value] = str(new)

    def _reconcile_orders(self) -> None:
        remaining: list[PersistedOrder] = []
        for item in self.state.orders:
            payload = self.trader.get_order(item.order_id)
            status = str(payload.get("status") or "").strip().lower()
            if status not in TERMINAL_ORDER_STATUSES:
                remaining.append(item)
        self.state.orders = remaining

    def _maker_state(self) -> MakerState:
        inventory = tuple(
            InventoryPosition(
                side,
                _decimal(self.state.balances.get(side.value)),
                _decimal(self.state.average_prices.get(side.value)),
                _decimal(self.state.first_fill_at.get(side.value)),
            )
            for side in OutcomeSide
            if _decimal(self.state.balances.get(side.value)) > Decimal("0.0001")
        )
        orders = tuple(
            WorkingOrder(
                item.order_id,
                OutcomeSide(item.outcome),
                OrderSide(item.order_side),
                _decimal(item.price),
                _decimal(item.shares),
                _decimal(item.created_at),
            )
            for item in self.state.orders
        )
        return MakerState(inventory, orders, _decimal(self.state.committed_notional))

    def _pause(self, slug: str, reason: str) -> None:
        self.state.paused_window = slug
        self.state.pause_reason = reason
        self.save()
