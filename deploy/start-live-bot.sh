#!/bin/zsh
set -euo pipefail

umask 077

readonly APP_ROOT="/Users/tianliuping/Library/Application Support/PolymarketTradingBot"
readonly RUNTIME="$APP_ROOT/runtime"
readonly DATA_DIR="$APP_ROOT/data"
readonly ENV_FILE="/Users/tianliuping/Documents/tradingbot/.env"
readonly PYTHON="/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
readonly PROXY_WAIT_SECONDS=60
readonly PROXY_HEALTH="$APP_ROOT/proxy-health.sh"

source "$PROXY_HEALTH"

now=$(/bin/date +%s)
slug=${1:-btc-updown-5m-$((now / 300 * 300))}
cd "$RUNTIME"

if ! proxy_wait_ready "$PROXY_WAIT_SECONDS" 0; then
  print -u2 -- "Local proxy did not become healthy within ${PROXY_WAIT_SECONDS}s; refusing to start live trading"
  exit 1
fi
if polymarket_proxy_is_eligible; then
  :
else
  status=$?
  if (( status == 2 )); then
    print -u2 -- "Polymarket proxy egress is geo-blocked; refusing to start live trading"
  else
    print -u2 -- "Polymarket proxy eligibility could not be verified; refusing to start live trading"
  fi
  exit 1
fi

export PYTHONUNBUFFERED=1
export TELEGRAM_PROXY="socks5h://127.0.0.1:7898"
export TELEGRAM_FALLBACK_PROXY="http://127.0.0.1:7897"
export TELEGRAM_TIMEOUT="20"
export TELEGRAM_TRADE_LEDGER="$DATA_DIR/live_trade_events.jsonl"
export TELEGRAM_DAILY_STATE="$DATA_DIR/telegram_daily_state.json"

exec "$PYTHON" -u -m src.watch_updown \
  --slug "$slug" \
  --duration 0 \
  --interval 5 \
  --auto-trade \
  --live-trading \
  --strategy reversal_three_16 \
  --price-source POLYMARKET_CHAINLINK \
  --crypto-resolution-mode auto \
  --ws-proxy http://127.0.0.1:7897 \
  --price-to-beat-proxy socks5h://127.0.0.1:7898 \
  --price-alignment-jsonl "$DATA_DIR/live_price_alignment.jsonl" \
  --max-price-alignment-difference 0.50 \
  --max-boundary-sample-offset-ms 1000 \
  --official-open-confirmations 2 \
  --official-open-stable-seconds 5 \
  --env-file "$ENV_FILE" \
  --reversal-state-json "$DATA_DIR/reversal_v11_state.json" \
  --reversal-first-stage-max-rv60 0.0020 \
  --reversal-first-stage-max-rv300 0.0050 \
  --decision-seconds-before-end 120 \
  --min-seconds-before-end 25 \
  --signal-confirmations 2 \
  --trend-confirmation-samples 3 \
  --one-way-entry-seconds 100 \
  --one-way-entry-cutoff-seconds 25 \
  --trend-pullback-tolerance-usd 1.00 \
  --trend-pullback-tolerance-percent 25 \
  --confirmation-jump-sigma-multiplier 1.25 \
  --confirmation-min-jump-usd 3.00 \
  --hedge-signal-confirmations 2 \
  --hedge-confirmation-min-seconds 2 \
  --hedge-max-price-worsening 0.05 \
  --hedge-min-win-probability 0.53 \
  --hedge-min-edge 0.01 \
  --hedge-fee-rate 0.07 \
  --hedge-entry-start-seconds 300 \
  --hedge-entry-cutoff-seconds 1 \
  --hedge-open-cross-min-usd 1.00 \
  --hedge-open-cross-sigma-multiplier 1.00 \
  --hedge-market-reversal-threshold 0.55 \
  --hedge-max-entry 0.99 \
  --hedge-max-spread 0.10 \
  --hedge-max-live-notional 0 \
  --final-poll-seconds 30 \
  --final-poll-interval 1 \
  --post-fill-poll-interval 1 \
  --pre-submit-max-adverse-ask-drop 0.02 \
  --pre-submit-max-ask-worsening 0.02 \
  --pre-submit-max-quote-age-seconds 1.0 \
  --min-win-probability 0.55 \
  --probability-shrinkage 1.00 \
  --fair-value-fee-rate 0.07 \
  --fair-value-confirmation-min-seconds 0 \
  --fair-value-book-trend-samples 3 \
  --fair-value-book-min-slope 0.003 \
  --fair-value-book-min-relative-slope 0.005 \
  --fair-value-book-max-pullback 0.01 \
  --edge 0.02 \
  --min-entry 0.50 \
  --low-entry-cutoff 0.55 \
  --low-entry-min-win-probability 0.61 \
  --low-entry-confirmation-samples 3 \
  --max-entry 0.82 \
  --max-spread 0.05 \
  --min-ask-sum 0.90 \
  --max-ask-sum 1.10 \
  --order-size 5 \
  --max-trades 2 \
  --max-live-orders 0 \
  --max-live-notional 4.25 \
  --live-order-type FAK \
  --live-buy-slippage 0.03 \
  --live-summary-json "$DATA_DIR/live_trade_summary.json"
