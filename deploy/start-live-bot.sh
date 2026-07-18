#!/bin/zsh
set -euo pipefail

now=$(/bin/date +%s)
slug=${1:-btc-updown-5m-$((now / 300 * 300))}
python3 -u -m src.watch_updown \
  --slug "$slug" \
  --duration 0 \
  --interval 5 \
  --auto-trade \
  --live-trading \
  --strategy fair_value_edge \
  --price-source POLYMARKET_CHAINLINK \
  --ws-proxy socks5h://127.0.0.1:7898 \
  --env-file .env \
  --decision-seconds-before-end 90 \
  --min-seconds-before-end 25 \
  --signal-confirmations 2 \
  --min-win-probability 0.62 \
  --probability-shrinkage 1.00 \
  --edge 0.06 \
  --min-entry 0.50 \
  --low-entry-cutoff 0.50 \
  --low-entry-min-win-probability 0.68 \
  --low-entry-confirmation-samples 3 \
  --max-entry 0.78 \
  --max-spread 0.04 \
  --min-ask-sum 0.90 \
  --max-ask-sum 1.10 \
  --order-size 5 \
  --max-trades 2 \
  --max-live-orders 0 \
  --max-live-notional 3.75 \
  --live-order-type FOK \
  --live-summary-json data/live_trade_summary.json
