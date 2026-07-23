from datetime import datetime, timezone
from decimal import Decimal

from src.price_alignment import PolymarketPriceToBeatClient, StableOpenPriceTracker


def test_fetch_price_to_beat_uses_five_minute_window(monkeypatch) -> None:
    captured = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "openPrice": 63967.95,
                "closePrice": 63999.99,
                "timestamp": 1784355812574,
                "completed": False,
                "incomplete": True,
            }

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("src.price_alignment.requests.get", fake_get)
    client = PolymarketPriceToBeatClient(timeout=3, proxy_url="socks5h://127.0.0.1:7898")
    result = client.fetch(
        datetime(2026, 7, 18, 6, 20, tzinfo=timezone.utc),
        datetime(2026, 7, 18, 6, 25, tzinfo=timezone.utc),
    )

    assert result.open_price == Decimal("63967.95")
    assert result.price_to_beat == Decimal("63967.95")
    assert result.incomplete is True
    assert captured["params"] == {
        "symbol": "BTC",
        "eventStartTime": "2026-07-18T06:20:00Z",
        "variant": "fiveminute",
        "endDate": "2026-07-18T06:25:00Z",
    }
    assert captured["proxies"]["https"] == "socks5h://127.0.0.1:7898"


def test_fetch_price_to_beat_requires_open_price(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"completed": False}

    monkeypatch.setattr("src.price_alignment.requests.get", lambda *args, **kwargs: Response())

    client = PolymarketPriceToBeatClient()
    try:
        client.fetch(
            datetime(2026, 7, 18, 6, 20, tzinfo=timezone.utc),
            datetime(2026, 7, 18, 6, 25, tzinfo=timezone.utc),
        )
    except ValueError as exc:
        assert "openPrice" in str(exc)
    else:
        raise AssertionError("missing openPrice must fail closed")


def test_stable_open_price_requires_matching_reads_across_minimum_duration() -> None:
    tracker = StableOpenPriceTracker(required_confirmations=2, minimum_stable_seconds=5.0)

    assert tracker.observe(Decimal("65851.00696"), 100.0) is None
    assert tracker.observe(Decimal("65861.35686"), 105.0) is None
    assert tracker.observe(Decimal("65861.35686"), 109.9) is None
    assert tracker.observe(Decimal("65861.35686"), 110.0) == Decimal("65861.35686")


def test_stable_open_price_reset_discards_preliminary_value() -> None:
    tracker = StableOpenPriceTracker(required_confirmations=2, minimum_stable_seconds=5.0)

    assert tracker.observe(Decimal("65851.00696"), 100.0) is None
    tracker.reset()
    assert tracker.observe(Decimal("65851.00696"), 106.0) is None
    assert tracker.observe(Decimal("65851.00696"), 111.0) == Decimal("65851.00696")
