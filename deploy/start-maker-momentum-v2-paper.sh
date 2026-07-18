#!/bin/zsh
set -euo pipefail

now=$(/bin/date +%s)
slug=${1:-btc-updown-5m-$((now / 300 * 300))}
python3 -u -m src.watch_updown \
  --slug "$slug" \
  --duration 28800 \
  --interval 2 \
  --price-source POLYMARKET_CHAINLINK \
  --ws-proxy socks5h://127.0.0.1:7898 \
  --market-data-timeout 3 \
  --auto-trade \
  --strategy maker_momentum \
  --max-spot-age 10 \
  --max-start-capture-delay 15 \
  --max-trades 1 \
  --paper-trading \
  --paper-bankroll 20 \
  --paper-stake 1 \
  --stop-when-bust \
  --momentum-target-pair-sum 1.02 \
  --momentum-start-delay-seconds 10 \
  --momentum-min-rest-seconds 4 \
  --momentum-reprice-ticks 2 \
  --momentum-confirmation-samples 1 \
  --momentum-trigger-timeout-seconds 8 \
  --momentum-min-seconds-before-end 30 \
  --momentum-min-entry 0.60 \
  --momentum-max-entry 0.88 \
  --momentum-min-probability 0.55 \
  --momentum-flow-probability-boost 0.10 \
  --momentum-min-expected-roi 0.03 \
  --momentum-min-lead-bps 0.50 \
  --momentum-strong-expected-roi 0.04 \
  --momentum-strong-lead-bps 2.00 \
  --momentum-spot-samples 3 \
  --momentum-max-chase 0.06 \
  --momentum-fee-rate 0.07 \
  --momentum-max-spread 0.02 \
  --momentum-min-ask-sum 0.97 \
  --momentum-max-ask-sum 1.03
