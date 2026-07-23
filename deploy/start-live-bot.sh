#!/bin/zsh
set -euo pipefail

umask 077

readonly APP_ROOT="/Users/tianliuping/Library/Application Support/PolymarketTradingBot"
readonly RUNTIME="$APP_ROOT/runtime"
readonly DATA_DIR="$APP_ROOT/data"
readonly ENV_FILE="/Users/tianliuping/Documents/tradingbot/.env"
readonly PYTHON="/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"

now=$(/bin/date +%s)
slug=${1:-btc-updown-5m-$((now / 300 * 300))}
cd "$RUNTIME"

export PYTHONUNBUFFERED=1
export TELEGRAM_TRADE_LEDGER="$DATA_DIR/live_trade_events.jsonl"
export TELEGRAM_DAILY_STATE="$DATA_DIR/telegram_daily_state.json"

exec "$PYTHON" -u -m src.watch_updown \
  --slug "$slug" \
  --duration 0 \
  --interval 5 \
  --auto-trade \
  --live-trading \
  --strategy fair_value_edge \
  --price-source POLYMARKET_CHAINLINK \
  --ws-proxy socks5h://127.0.0.1:7898 \
  --price-to-beat-proxy socks5h://127.0.0.1:7898 \
  --price-alignment-jsonl "$DATA_DIR/live_price_alignment.jsonl" \
  --max-price-alignment-difference 0.50 \
  --max-boundary-sample-offset-ms 1000 \
  --official-open-confirmations 2 \
  --official-open-stable-seconds 5 \
  --env-file "$ENV_FILE" \
  --decision-seconds-before-end 120 \
  --min-seconds-before-end 25 \
  --signal-confirmations 2 \
  --trend-confirmation-samples 3 \
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
  --min-win-probability 0.55 \
  --probability-shrinkage 1.00 \
  --edge 0.02 \
  --min-entry 0.50 \
  --low-entry-cutoff 0.55 \
  --low-entry-min-win-probability 0.61 \
  --low-entry-confirmation-samples 3 \
  --max-entry 0.78 \
  --max-spread 0.05 \
  --min-ask-sum 0.90 \
  --max-ask-sum 1.10 \
  --order-size 5 \
  --max-trades 2 \
  --max-live-orders 0 \
  --max-live-notional 4.05 \
  --late-max-live-notional 4.70 \
  --live-order-type FAK \
  --live-buy-slippage 0.03 \
  --live-summary-json "$DATA_DIR/live_trade_summary.json"
