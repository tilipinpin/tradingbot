from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Display the latest live trading session summary.")
    parser.add_argument("path", nargs="?", default="data/live_trade_summary.json")
    return parser.parse_args()


def main() -> None:
    path = Path(parse_args().path)
    if not path.exists():
        raise SystemExit(f"No live trade summary found: {path}")
    payload = json.loads(path.read_text())
    order = payload.get("order") or {}
    orders = payload.get("orders") or []
    print(f"status: {payload.get('status')}")
    print(f"started_at: {payload.get('started_at')}")
    print(f"finished_at: {payload.get('finished_at')}")
    print(f"mode: {payload.get('mode') or payload.get('strategy')}")
    print(f"order_attempts: {payload.get('order_attempts', len(orders))}")
    if payload.get("matched_orders") is not None:
        print(f"matched_orders: {payload.get('matched_orders')}")
    if order:
        print(
            "order: "
            f"{order.get('slug')} {order.get('side')} "
            f"@ {order.get('price')} x {order.get('size')} "
            f"({order.get('order_type')})"
        )
    if payload.get("response") is not None:
        print("response: " + json.dumps(payload["response"], ensure_ascii=False, sort_keys=True))
    if payload.get("error"):
        print(f"error: {payload['error']}")
    for item in orders:
        response = item.get("response") or {}
        print(
            f"{item.get('side')}: status={response.get('status')} "
            f"success={response.get('success')} order_id={response.get('orderID')}"
        )
        if item.get("error"):
            print(f"{item.get('side')} error: {item.get('error')}")
    if payload.get("collateral_before") is not None:
        print(f"collateral: {payload.get('collateral_before')} -> {payload.get('collateral_after')}")
    if payload.get("position_after_buy") is not None:
        print(
            f"position: {payload.get('position_after_buy')} -> "
            f"{payload.get('position_after_sell')}"
        )


if __name__ == "__main__":
    main()
