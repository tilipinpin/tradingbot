#!/bin/zsh
set -euo pipefail

now=$(/bin/date +%s)
slug=${1:-btc-updown-5m-$((now / 300 * 300))}
python3 -u -m src.watch_updown \
  --slug "$slug" \
  --duration 28800 \
  --interval 5 \
  --price-source POLYMARKET_CHAINLINK \
  --ws-proxy socks5h://127.0.0.1:7898 \
  --official-price-to-beat \
  --price-to-beat-proxy socks5h://127.0.0.1:7898 \
  --price-alignment-jsonl data/late_favorite_v5_official_alignment.jsonl \
  --max-price-alignment-difference 0.50 \
  --max-boundary-sample-offset-ms 1000 \
  --market-data-timeout 3 \
  --auto-trade \
  --strategy late_favorite \
  --max-spot-age 5 \
  --max-start-capture-delay 15 \
  --max-trades 1 \
  --paper-trading \
  --paper-bankroll 20 \
  --paper-stake 1 \
  --stop-when-bust \
  --late-entry-start-seconds 55 \
  --late-entry-cutoff-seconds 8 \
  --late-min-entry 0.65 \
  --late-max-entry 0.94 \
  --late-min-win-probability 0.80 \
  --late-edge-margin 0.00 \
  --late-min-expected-roi 0.02 \
  --late-fee-rate 0.07 \
  --late-max-spread 0.03 \
  --late-min-ask-sum 0.96 \
  --late-max-ask-sum 1.04 \
  --late-confirmation-samples 2 \
  --late-no-cross-samples 3 \
  --late-signal-confirmations 1 \
  --late-min-lead-bps 1.0 \
  --late-max-pullback-bps 1.50 \
  --late-max-pullback-ratio 0.50 \
  --late-volatility-buffer-multiplier 0.50 \
  --late-pause-windows-after-loss 0
