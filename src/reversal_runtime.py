from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol

from src.polymarket import Market, OrderBookSnapshot
from src.polygon_split import CompleteSetSplitter
from src.reversal_v11 import (
    Direction,
    MarketHealth,
    ReversalV11,
    TradePlan,
    format_daily_report,
)


class ReversalRuntimeError(RuntimeError):
    pass


class Splitter(Protocol):
    def split(
        self,
        *,
        condition_id: str,
        up_token_id: str,
        down_token_id: str,
        amount: Decimal,
        neg_risk: bool,
    ) -> Any: ...

    def merge(
        self,
        *,
        condition_id: str,
        up_token_id: str,
        down_token_id: str,
        amount: Decimal,
        neg_risk: bool,
    ) -> Any: ...


class ExitTrader(Protocol):
    def sell_limit(
        self,
        token_id: str,
        price: Decimal,
        size: Decimal,
        tick_size: str,
        neg_risk: bool,
        order_type: str = "GTC",
        submit_not_after_monotonic: float | None = None,
    ) -> dict[str, Any]: ...

    def conditional_balance(self, token_id: str, signature_type: int) -> Decimal: ...


@dataclass(frozen=True)
class ReversalTickResult:
    status: str
    plan: TradePlan | None = None
    order: dict[str, Any] | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ReversalStartupReport:
    wallet: str
    collateral_units: int
    open_orders: int
    up_balance: Decimal
    down_balance: Decimal
    relayer_deployed: bool


def reversal_startup_self_check(
    *,
    market: Market,
    splitter: CompleteSetSplitter,
    trader: ExitTrader,
    signature_type: int,
    required_collateral: Decimal = Decimal("30"),
) -> ReversalStartupReport:
    if signature_type != 3:
        raise ReversalRuntimeError("reversal_v11 live execution requires SIGNATURE_TYPE=3")
    check_amount = max(Decimal("0.000001"), required_collateral)
    preflight = splitter.preflight(
        condition_id=market.condition_id,
        up_token_id=market.token_ids[0],
        down_token_id=market.token_ids[1],
        amount=check_amount,
        neg_risk=market.neg_risk,
    )
    open_orders = trader.open_orders()  # type: ignore[attr-defined]
    if open_orders:
        raise ReversalRuntimeError(
            f"startup blocked: {len(open_orders)} existing CLOB order(s) require review"
        )
    up_balance = trader.conditional_balance(market.token_ids[0], signature_type)
    down_balance = trader.conditional_balance(market.token_ids[1], signature_type)
    relayer_check = getattr(splitter.submitter, "read_only_self_check", None)
    if relayer_check is None:
        raise ReversalRuntimeError("configured split submitter has no read-only Relayer self-check")
    relayer = relayer_check()
    if not relayer.get("deployed"):
        raise ReversalRuntimeError("Relayer reports that the Deposit Wallet is not deployed")
    return ReversalStartupReport(
        wallet=preflight.wallet,
        collateral_units=preflight.collateral_balance_units,
        open_orders=0,
        up_balance=up_balance,
        down_balance=down_balance,
        relayer_deployed=True,
    )


def previous_5m_slug(slug: str, count: int = 1) -> str:
    prefix, separator, raw_epoch = slug.rpartition("-")
    if not separator or not raw_epoch.isdigit() or count < 1:
        raise ValueError(f"invalid five-minute slug: {slug}")
    return f"{prefix}-{int(raw_epoch) - 300 * count}"


def slug_epoch(slug: str) -> int:
    raw = slug.rpartition("-")[2]
    if not raw.isdigit():
        raise ValueError(f"invalid five-minute slug: {slug}")
    return int(raw)


def bid_depth(book: OrderBookSnapshot, minimum_price: Decimal = Decimal("0")) -> Decimal:
    return sum(
        (level.size for level in book.bids if level.price >= minimum_price and level.size > 0),
        Decimal("0"),
    )


def market_health_from_books(
    *,
    trend_side: Direction,
    up_book: OrderBookSnapshot,
    down_book: OrderBookSnapshot,
    making_amount: Decimal,
    spot_prices: list[Decimal],
    open_price: Decimal,
    market_data_ok: bool = True,
    trading_api_ok: bool = True,
) -> MarketHealth:
    trend_book = up_book if trend_side is Direction.UP else down_book
    quote = trend_book.quote
    spread = (
        quote.ask - quote.bid
        if quote.ask is not None and quote.bid is not None
        else Decimal("1")
    )
    if open_price <= 0:
        absolute_move = Decimal("1")
    else:
        latest = spot_prices[-1] if spot_prices else open_price
        absolute_move = abs(latest - open_price) / open_price
    returns = [
        abs(current - previous) / previous
        for previous, current in zip(spot_prices, spot_prices[1:])
        if previous > 0
    ]
    short_volatility = max(returns[-5:], default=Decimal("0"))
    depth = bid_depth(trend_book)
    return MarketHealth(
        short_volatility=short_volatility,
        absolute_window_move=absolute_move,
        trend_bid_depth=depth,
        trend_spread=spread,
        estimated_sellable=quote.bid is not None and depth >= making_amount,
        market_data_ok=market_data_ok,
        trading_api_ok=trading_api_ok,
    )


class ReversalRuntime:
    """Crash-safe V1.1 window orchestration around the pure strategy state machine."""

    def __init__(
        self,
        *,
        strategy: ReversalV11,
        state_path: Path,
        winner_lookup: Callable[[str], str | None],
        splitter: Splitter | None,
        trader: ExitTrader | None,
        signature_type: int,
        live: bool,
        order_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.strategy = strategy
        self.state_path = state_path
        self.winner_lookup = winner_lookup
        self.splitter = splitter
        self.trader = trader
        self.signature_type = signature_type
        self.live = live
        self.order_callback = order_callback

    def save(self) -> None:
        self.strategy.dump(self.state_path)

    def send_daily_report_once(
        self,
        report_day: date,
        sender: Callable[[str], bool],
    ) -> bool:
        key = report_day.isoformat()
        if key in self.strategy.state.reported_days:
            return False
        if not sender(format_daily_report(report_day, self.strategy.metrics(report_day))):
            return False
        self.strategy.state.reported_days = (
            self.strategy.state.reported_days + [key]
        )[-14:]
        self.save()
        return True

    def sync_settlements(self, current_slug: str) -> list[tuple[str, str]]:
        current_epoch = slug_epoch(current_slug)
        candidates = {
            previous_5m_slug(current_slug, 2),
            previous_5m_slug(current_slug, 1),
        }
        active = self.strategy.state.active_round
        if active is not None and active.awaiting_window is not None:
            if slug_epoch(active.awaiting_window) < current_epoch:
                candidates.add(active.awaiting_window)
        last_epoch = (
            slug_epoch(self.strategy.state.last_settled_slug)
            if self.strategy.state.last_settled_slug
            else None
        )
        observed: list[tuple[str, str]] = []
        for candidate in sorted(candidates, key=slug_epoch):
            candidate_epoch = slug_epoch(candidate)
            if candidate_epoch >= current_epoch:
                continue
            if last_epoch is not None and candidate_epoch <= last_epoch:
                continue
            winner = self.winner_lookup(candidate)
            if winner not in {Direction.UP.value, Direction.DOWN.value}:
                break
            outcome = self.strategy.settle_window(candidate, winner)
            self.save()
            observed.append((candidate, outcome))
            last_epoch = candidate_epoch
        return observed

    def tick(
        self,
        *,
        market: Market,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
        health: MarketHealth | None = None,
        health_by_side: dict[Direction, MarketHealth] | None = None,
        book_refresh: Callable[[], tuple[OrderBookSnapshot, OrderBookSnapshot]]
        | None = None,
    ) -> ReversalTickResult:
        existing_active = self.strategy.state.active_round
        if (
            existing_active is not None
            and existing_active.awaiting_window == market.slug
            and existing_active.execution_phase
            in {
                "split_confirmed",
                "trend_exit_partial",
                "trend_exit_submitting",
                "trend_exit_complete",
            }
        ):
            selected = (
                health_by_side.get(existing_active.trend_side)
                if health_by_side is not None
                else health
            )
            if selected is None:
                raise ReversalRuntimeError("no market-health snapshot for active trend side")
            resumed_plan = self.strategy.plan_window(market.slug, selected)
            assert resumed_plan is not None
            return self._finish_plan(
                market=market,
                up_book=up_book,
                down_book=down_book,
                plan=resumed_plan,
            )
        if (
            self.strategy.state.prepared_split is None
            and self.strategy.state.last_opening_processed_slug == market.slug
        ):
            return ReversalTickResult("opening_already_processed")
        self.sync_settlements(market.slug)
        if not self._immediate_results_available(market.slug):
            return ReversalTickResult(
                "waiting_result_no_split",
                detail="no chain transaction submitted",
            )

        trend_side = self._confirmed_trend_side()
        prepared = self.strategy.state.prepared_split
        if trend_side is None and prepared is None:
            self.strategy.state.last_opening_processed_slug = market.slug
            self.save()
            return ReversalTickResult(
                "no_trigger_no_split",
                detail="last two settled windows differ",
            )
        selected_health = (
            health_by_side.get(trend_side)
            if health_by_side is not None and trend_side is not None
            else health
        )
        if selected_health is None and health_by_side:
            selected_health = next(iter(health_by_side.values()))
        if selected_health is None:
            raise ReversalRuntimeError("no market-health snapshot for confirmed trend side")
        plan = self.strategy.plan_window(market.slug, selected_health)
        if plan is None:
            if prepared is None:
                return ReversalTickResult(
                    "trigger_filtered_no_split",
                    detail="market-health filter blocked entry",
                )
            return self._merge_unused_opening_split(market, prepared.amount)

        if prepared is None:
            prepared = self.strategy.prepare_opening_split(market.slug)
            if prepared.amount != plan.making_amount:
                raise ReversalRuntimeError(
                    "prepared split amount does not match confirmed trade plan"
                )
            self.save()
        if prepared.execution_phase in {"split_submitting", "split_uncertain"}:
            raise ReversalRuntimeError(
                "opening split outcome is uncertain; automatic retry is blocked pending reconciliation"
            )
        if prepared.execution_phase in {"merge_submitting", "merge_uncertain"}:
            raise ReversalRuntimeError(
                "opening merge outcome is uncertain; automatic retry is blocked pending reconciliation"
            )
        if prepared.execution_phase == "planned":
            self.strategy.mark_opening_split_submitting()
            self.save()
            if self.live:
                if self.splitter is None or self.trader is None:
                    raise ReversalRuntimeError(
                        "live reversal runtime requires splitter and trader"
                    )
                try:
                    receipt = self.splitter.split(
                        condition_id=market.condition_id,
                        up_token_id=market.token_ids[0],
                        down_token_id=market.token_ids[1],
                        amount=prepared.amount,
                        neg_risk=market.neg_risk,
                    )
                except Exception:
                    self.strategy.mark_opening_split_uncertain()
                    self.save()
                    raise
                transaction_hash = str(receipt.transaction_hash)
            else:
                transaction_hash = "paper-opening-split"
            self.strategy.mark_opening_split_confirmed(transaction_hash)
            self.save()

        self.strategy.adopt_opening_split(plan)
        self.save()
        if book_refresh is not None:
            up_book, down_book = book_refresh()
        return self._finish_plan(
            market=market,
            up_book=up_book,
            down_book=down_book,
            plan=plan,
        )

    def _merge_unused_opening_split(
        self,
        market: Market,
        amount: Decimal,
    ) -> ReversalTickResult:
        if not self.live:
            self.strategy.mark_opening_merge_submitting()
            self.strategy.mark_opening_merge_confirmed()
            self.save()
            return ReversalTickResult("paper_unused_split_merged")
        if self.splitter is None:
            raise ReversalRuntimeError("live reversal merge requires splitter")
        self.strategy.mark_opening_merge_submitting()
        self.save()
        try:
            receipt = self.splitter.merge(
                condition_id=market.condition_id,
                up_token_id=market.token_ids[0],
                down_token_id=market.token_ids[1],
                amount=amount,
                neg_risk=market.neg_risk,
            )
        except Exception:
            self.strategy.mark_opening_merge_uncertain()
            self.save()
            raise
        self.strategy.mark_opening_merge_confirmed()
        self.save()
        return ReversalTickResult(
            "unused_split_merged",
            detail=f"tx={receipt.transaction_hash}",
        )

    def _finish_plan(
        self,
        *,
        market: Market,
        up_book: OrderBookSnapshot,
        down_book: OrderBookSnapshot,
        plan: TradePlan,
    ) -> ReversalTickResult:
        active = self.strategy.state.active_round
        assert active is not None
        if not self.live:
            if active.execution_phase != "trend_exit_complete":
                remaining = max(
                    Decimal("0"), plan.making_amount - active.exit_sold_shares
                )
                self.strategy.record_exit_fill(
                    plan,
                    shares=remaining,
                    proceeds=remaining * Decimal("0.50"),
                )
                self.save()
            return ReversalTickResult("paper_complete", plan=plan)

        if self.trader is None:
            raise ReversalRuntimeError("live reversal runtime requires trader")

        active = self.strategy.state.active_round
        assert active is not None
        if active.execution_phase == "trend_exit_complete":
            return ReversalTickResult("awaiting_settlement", plan=plan)

        trend_index = 0 if plan.trend_side is Direction.UP else 1
        trend_token = market.token_ids[trend_index]
        trend_book = up_book if trend_index == 0 else down_book
        available = self.trader.conditional_balance(trend_token, self.signature_type)
        expected_remaining = max(
            Decimal("0"), plan.making_amount - active.exit_sold_shares
        )
        if (
            available <= Decimal("0.000001")
            and active.execution_phase == "trend_exit_submitting"
        ):
            self.strategy.reconcile_exit_complete(plan)
            self.save()
            return ReversalTickResult("exit_reconciled", plan=plan)
        sell_size = min(expected_remaining, available)
        if sell_size <= 0:
            return ReversalTickResult(
                "exit_balance_pending", plan=plan, detail=f"available={available}"
            )
        best_bid = trend_book.quote.bid
        if best_bid is None or best_bid <= 0:
            return ReversalTickResult("exit_book_pending", plan=plan)

        prices = [best_bid]
        latest_order: dict[str, Any] | None = None
        for price in prices:
            if active.execution_phase == "trend_exit_submitting":
                self.strategy.mark_exit_retryable(plan)
            self.strategy.mark_exit_submitting(plan)
            self.save()
            response = self.trader.sell_limit(
                trend_token,
                price,
                sell_size,
                market.minimum_tick_size,
                market.neg_risk,
                "FAK",
            )
            latest_order = {
                "slug": market.slug,
                "side": plan.trend_side.value,
                "token_id": trend_token,
                "price": str(price),
                "size": str(sell_size),
                "order_type": "FAK",
                "order_role": "reversal_trend_exit",
                "round_id": plan.round_id,
                "attempt": plan.attempt,
                "retained_side": plan.retained_side.value,
                "split_amount": str(plan.making_amount),
                "split_transaction_hash": active.split_transaction_hash,
                "response": response,
            }
            shares, proceeds = _sell_fill_amounts(response)
            if shares > 0:
                complete = self.strategy.record_exit_fill(
                    plan, shares=shares, proceeds=proceeds
                )
                refreshed_active = self.strategy.state.active_round
                assert refreshed_active is not None
                latest_order["exit_complete"] = complete
                latest_order["cumulative_exit_proceeds"] = str(
                    refreshed_active.exit_sell_proceeds
                )
                self.save()
                if self.order_callback is not None:
                    self.order_callback(latest_order)
                return ReversalTickResult(
                    "exit_complete" if complete else "exit_partial",
                    plan=plan,
                    order=latest_order,
                )
            self.strategy.mark_exit_retryable(plan)
            self.save()
        self.strategy.record_execution(unmatched_orders=1)
        self.save()
        return ReversalTickResult("exit_unmatched", plan=plan, order=latest_order)

    def _immediate_results_available(self, current_slug: str) -> bool:
        expected = [previous_5m_slug(current_slug, 2), previous_5m_slug(current_slug)]
        return self.strategy.state.recent_slugs[-2:] == expected

    def _confirmed_trend_side(self) -> Direction | None:
        active = self.strategy.state.active_round
        if active is not None:
            return active.trend_side
        results = self.strategy.state.recent_results
        if len(results) >= 2 and results[-1] == results[-2]:
            return results[-1]
        return None


def _sell_fill_amounts(response: dict[str, Any]) -> tuple[Decimal, Decimal]:
    if not isinstance(response, dict) or str(response.get("status", "")).lower() != "matched":
        return Decimal("0"), Decimal("0")
    try:
        shares = Decimal(str(response.get("makingAmount") or "0"))
        proceeds = Decimal(str(response.get("takingAmount") or "0"))
    except (ArithmeticError, ValueError):
        return Decimal("0"), Decimal("0")
    if shares <= 0 or proceeds <= 0:
        return Decimal("0"), Decimal("0")
    return shares, proceeds
