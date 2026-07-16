from decimal import Decimal

from src.live_round_trip import _balance_units, _matched, _positive_allowance


def test_matched_requires_success_status_and_order_id() -> None:
    assert _matched({"success": True, "status": "matched", "orderID": "0x1"}) is True
    assert _matched({"success": True, "status": "live", "orderID": "0x1"}) is False
    assert _matched({"success": False, "status": "matched", "orderID": "0x1"}) is False
    assert _matched({"success": True, "status": "matched"}) is False


def test_balance_helpers_use_fixed_six_decimal_units() -> None:
    assert _balance_units({"balance": "5000000"}) == Decimal("5")
    assert _positive_allowance({"allowances": {"exchange": "1"}}) is True
    assert _positive_allowance({"allowances": {"exchange": "0"}}) is False
