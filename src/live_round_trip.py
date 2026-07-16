from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values
from py_clob_client_v2 import AssetType, BalanceAllowanceParams

from src.polymarket import ClobTradingClient, GammaClient, Market


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Buy and immediately sell a minimal UP position.")
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--size", default="5")
    parser.add_argument("--max-buy-notional", default="3.50")
    parser.add_argument("--max-spread", default="0.03")
    parser.add_argument("--min-seconds-left", type=int, default=180)
    parser.add_argument("--wait-seconds", type=int, default=600)
    parser.add_argument("--summary", default="data/live_trade_summary.json")
    return parser.parse_args()


def _write_summary(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _matched(response: Any) -> bool:
    return (
        isinstance(response, dict)
        and response.get("success") is True
        and str(response.get("status", "")).lower() == "matched"
        and bool(response.get("orderID"))
    )


def _balance_units(response: dict[str, Any]) -> Decimal:
    return Decimal(str(response.get("balance") or "0")) / Decimal("1000000")


def _balance_params(asset_type: str, signature_type: int, token_id: str | None = None) -> Any:
    return BalanceAllowanceParams(
        asset_type=asset_type,
        token_id=token_id,
        signature_type=signature_type,
    )


def _positive_allowance(response: dict[str, Any]) -> bool:
    return any(Decimal(str(value)) > 0 for value in (response.get("allowances") or {}).values())


def _current_or_next_market(gamma: GammaClient, min_seconds_left: int, deadline: float) -> Market:
    while time.monotonic() < deadline:
        now = int(time.time())
        start = (now // 300) * 300
        if start + 300 - now < min_seconds_left:
            start += 300
        slug = f"btc-updown-5m-{start}"
        try:
            market = gamma.market_by_slug(slug)
        except (LookupError, requests.RequestException):
            time.sleep(2)
            continue
        if market.event_start_time is None or market.end_time is None:
            time.sleep(2)
            continue
        seconds_to_start = market.event_start_time.timestamp() - time.time()
        if seconds_to_start > 0:
            time.sleep(min(2, max(0.2, seconds_to_start)))
            continue
        if market.end_time.timestamp() - time.time() >= min_seconds_left:
            return market
        time.sleep(2)
    raise TimeoutError("No full BTC 5-minute market became available before the deadline")


def _wait_for_position(
    trader: ClobTradingClient,
    params: Any,
    minimum: Decimal,
    timeout: int = 20,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        trader.client.update_balance_allowance(params)
        latest = trader.client.get_balance_allowance(params)
        if _balance_units(latest) >= minimum and _positive_allowance(latest):
            return latest
        time.sleep(1)
    return latest


def _wait_for_position_at_most(
    trader: ClobTradingClient,
    params: Any,
    maximum: Decimal,
    timeout: int = 20,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        trader.client.update_balance_allowance(params)
        latest = trader.client.get_balance_allowance(params)
        if _balance_units(latest) <= maximum:
            return latest
        time.sleep(1)
    return latest


def main() -> None:
    args = parse_args()
    env = dotenv_values(args.env_file)
    size = Decimal(args.size)
    max_notional = Decimal(args.max_buy_notional)
    max_spread = Decimal(args.max_spread)
    signature_type = int(env.get("SIGNATURE_TYPE") or "3")
    summary_path = Path(args.summary)
    summary: dict[str, Any] = {
        "status": "preflight",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live_round_trip",
        "size": str(size),
        "max_buy_notional": str(max_notional),
        "orders": [],
    }
    _write_summary(summary_path, summary)

    buy_matched = False
    try:
        geoblock = requests.get("https://polymarket.com/api/geoblock", timeout=20).json()
        summary["geoblock"] = geoblock
        if geoblock.get("blocked") is not False:
            raise RuntimeError(f"Polymarket geoblock did not return blocked=false: {geoblock}")

        host = env.get("CLOB_HOST") or "https://clob.polymarket.com"
        trader = ClobTradingClient(
            host,
            int(env.get("CHAIN_ID") or "137"),
            str(env["PRIVATE_KEY"]),
            str(env["DEPOSIT_WALLET"]),
            signature_type,
        )
        collateral_params = _balance_params(AssetType.COLLATERAL, signature_type)
        collateral_before = trader.client.get_balance_allowance(collateral_params)
        summary["collateral_before"] = str(_balance_units(collateral_before))
        summary["open_orders_before"] = len(trader.client.get_open_orders())
        if summary["open_orders_before"] != 0:
            raise RuntimeError("Open orders exist; refusing one-shot connectivity test")
        if _balance_units(collateral_before) < max_notional:
            raise RuntimeError("Collateral balance is below the configured maximum buy notional")
        if not _positive_allowance(collateral_before):
            raise RuntimeError("Collateral allowance is zero")

        deadline = time.monotonic() + args.wait_seconds
        gamma = GammaClient()
        while True:
            market = _current_or_next_market(gamma, args.min_seconds_left, deadline)
            up_quote = trader.quote(market.token_ids[0])
            if up_quote.bid is None or up_quote.ask is None:
                time.sleep(1)
                continue
            spread = up_quote.ask - up_quote.bid
            tick = Decimal(market.minimum_tick_size)
            buy_price = up_quote.ask + tick
            if spread <= max_spread and buy_price * size <= max_notional:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("No UP quote passed spread and notional guards")
            time.sleep(1)

        summary.update(
            {
                "status": "buy_submitting",
                "slug": market.slug,
                "token_id": market.token_ids[0],
                "quote_before_buy": {"bid": str(up_quote.bid), "ask": str(up_quote.ask)},
                "buy_price": str(buy_price),
            }
        )
        _write_summary(summary_path, summary)
        buy_response = trader.buy_limit(
            market.token_ids[0],
            buy_price,
            size,
            market.minimum_tick_size,
            market.neg_risk,
            "FOK",
        )
        summary["orders"].append({"side": "BUY", "response": buy_response})
        if not _matched(buy_response):
            raise RuntimeError(f"BUY was not conclusively matched: {buy_response}")
        buy_matched = True
        summary["status"] = "buy_matched"
        _write_summary(summary_path, summary)

        conditional_params = _balance_params(
            AssetType.CONDITIONAL,
            signature_type,
            market.token_ids[0],
        )
        position = _wait_for_position(trader, conditional_params, size)
        position_size = _balance_units(position)
        summary["position_after_buy"] = str(position_size)
        if position_size < size:
            raise RuntimeError(f"Matched BUY position did not become sellable: {position_size} < {size}")

        sell_quote = trader.quote(market.token_ids[0])
        if sell_quote.bid is None:
            raise RuntimeError("No UP bid available after BUY")
        sell_price = max(Decimal(market.minimum_tick_size), sell_quote.bid - Decimal(market.minimum_tick_size))
        summary.update(
            {
                "status": "sell_submitting",
                "quote_before_sell": {"bid": str(sell_quote.bid), "ask": str(sell_quote.ask)},
                "sell_price": str(sell_price),
            }
        )
        _write_summary(summary_path, summary)
        sell_response = trader.sell_limit(
            market.token_ids[0],
            sell_price,
            size,
            market.minimum_tick_size,
            market.neg_risk,
            "FOK",
        )
        summary["orders"].append({"side": "SELL", "response": sell_response})
        if not _matched(sell_response):
            raise RuntimeError(f"SELL was not conclusively matched: {sell_response}")

        expected_remaining = max(Decimal("0"), position_size - size)
        position_after = _wait_for_position_at_most(
            trader,
            conditional_params,
            expected_remaining + Decimal("0.000001"),
        )
        if _balance_units(position_after) > expected_remaining + Decimal("0.000001"):
            raise RuntimeError("Matched SELL did not appear in the refreshed conditional balance")
        trader.client.update_balance_allowance(collateral_params)
        collateral_after = trader.client.get_balance_allowance(collateral_params)
        summary.update(
            {
                "status": "round_trip_completed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "collateral_after": str(_balance_units(collateral_after)),
                "position_after_sell": str(_balance_units(position_after)),
                "residual_position": str(_balance_units(position_after)),
                "open_orders_after": len(trader.client.get_open_orders()),
            }
        )
        _write_summary(summary_path, summary)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        summary["status"] = "buy_matched_sell_failed" if buy_matched else "stopped_before_position"
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["error"] = str(exc)
        _write_summary(summary_path, summary)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        raise


if __name__ == "__main__":
    main()
