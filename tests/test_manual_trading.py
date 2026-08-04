from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.manual_trading import (
    MANUAL_BUY_SHARES,
    ManualTradeExecutor,
    ManualTradeRequest,
    next_window_slug,
)
from src.polymarket import Market, OrderBookLevel, OrderBookSnapshot


NOW = 1_000.0


def market() -> Market:
    return Market(
        "BTC Up or Down",
        "btc-updown-5m-900",
        "0xcondition",
        ("up-token", "down-token"),
        "0.01",
        False,
        Decimal("100"),
        ("Up", "Down"),
        datetime.fromtimestamp(900, timezone.utc),
        datetime.fromtimestamp(1200, timezone.utc),
    )


def book(token: str, bid: str, ask: str) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token,
        timestamp="",
        bids=(OrderBookLevel(Decimal(bid), Decimal("100")),),
        asks=(OrderBookLevel(Decimal(ask), Decimal("100")),),
        minimum_order_size=Decimal("1"),
    )


class FakeTrader:
    def __init__(self) -> None:
        self.balance = Decimal("100")
        self.positions = {"up-token": Decimal("7.239"), "down-token": Decimal("0")}
        self.calls = []

    def collateral_balance(self, signature_type):
        return self.balance

    def conditional_balance(self, token_id, signature_type):
        return self.positions[token_id]

    def buy_limit(self, token_id, price, size, tick_size, neg_risk, order_type, **kwargs):
        self.calls.append(("buy", token_id, price, size, order_type))
        return {
            "status": "matched",
            "makingAmount": str(price * size),
            "takingAmount": str(size),
            "orderID": "buy-order",
        }

    def sell_limit(self, token_id, price, size, tick_size, neg_risk, order_type, **kwargs):
        self.calls.append(("sell", token_id, price, size, order_type))
        return {
            "status": "matched",
            "makingAmount": str(size),
            "takingAmount": str(price * size),
            "orderID": "sell-order",
        }


def executor(trader: FakeTrader) -> ManualTradeExecutor:
    return ManualTradeExecutor(
        trader=trader,
        signature_type=3,
        market_loader=lambda slug: market(),
        book_loader=lambda token_ids: (
            book("up-token", "0.59", "0.60"),
            book("down-token", "0.39", "0.40"),
        ),
        on_submitting=lambda request: None,
        on_result=lambda result: None,
        time_fn=lambda: NOW,
    )


def request(action: str, side: str) -> ManualTradeRequest:
    return ManualTradeRequest(
        request_id=f"{action}-{side}",
        target_slug="btc-updown-5m-900",
        action=action,
        side=side,
        requested_at=datetime.now(timezone.utc).isoformat(),
        sell_size=Decimal("7.239") if action == "sell" else None,
    )


def test_manual_buy_is_exactly_two_shares_per_click() -> None:
    trader = FakeTrader()

    result = executor(trader).execute(request("buy", "DOWN"))

    assert result.status == "matched"
    assert result.requested_size == MANUAL_BUY_SHARES == Decimal("2")
    assert trader.calls == [("buy", "down-token", Decimal("0.43"), Decimal("2"), "FAK")]


def test_manual_sell_uses_all_exchange_valid_available_shares() -> None:
    trader = FakeTrader()

    result = executor(trader).execute(request("sell", "UP"))

    assert result.status == "matched"
    assert result.requested_size == Decimal("7.23")
    assert result.remaining_size == Decimal("0.009")
    assert trader.calls == [("sell", "up-token", Decimal("0.56"), Decimal("7.23"), "FAK")]


def test_manual_order_expires_at_window_end() -> None:
    trader = FakeTrader()
    target = request("buy", "UP")
    service = executor(trader)
    service.time_fn = lambda: 1200.0

    result = service.execute(target)

    assert result.status == "expired"
    assert trader.calls == []


def test_next_window_slug_advances_exactly_five_minutes() -> None:
    assert next_window_slug("btc-updown-5m-900") == "btc-updown-5m-1200"
