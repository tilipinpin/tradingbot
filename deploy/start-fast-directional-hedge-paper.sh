#!/bin/zsh
set -euo pipefail

umask 077

readonly PROJECT_ROOT="/Users/tianliuping/.codex/worktrees/b0a7/tradingbot"
readonly DATA_DIR="$PROJECT_ROOT/data"
readonly ENV_FILE="/Users/tianliuping/Documents/tradingbot/.env"
readonly PYTHON="/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"

now=$(/bin/date +%s)
slug=${1:-btc-updown-5m-$((now / 300 * 300))}

cd "$PROJECT_ROOT"
/bin/mkdir -p "$DATA_DIR"

export PYTHONUNBUFFERED=1
export TELEGRAM_ENABLED=true
export TELEGRAM_COMMANDS_ENABLED=false
export DISCORD_ENABLED=false
export TELEGRAM_TRADE_LEDGER="$DATA_DIR/fast_directional_hedge_simple_paper.trade_events.jsonl"
export TELEGRAM_DAILY_STATE="$DATA_DIR/fast_directional_hedge_simple_paper.notification_state.json"

exec "$PYTHON" -u -m src.watch_updown \
  --slug "$slug" \
  --duration 0 \
  --interval 1 \
  --auto-trade \
  --paper-trading \
  --strategy fast_directional_hedge_simple \
  --paper-bankroll 50 \
  --paper-stake 1 \
  --paper-shares 0 \
  --stop-when-bust \
  --max-trades 8 \
  --max-consecutive-losses 0 \
  --pause-windows-after-losses 0 \
  --price-source POLYMARKET_CHAINLINK \
  --crypto-resolution-mode auto \
  --ws-proxy http://127.0.0.1:7897 \
  --price-to-beat-proxy socks5h://127.0.0.1:7898 \
  --price-alignment-jsonl "$DATA_DIR/fast_directional_hedge_simple_paper.alignment.jsonl" \
  --record-jsonl "$DATA_DIR/fast_directional_hedge_simple_paper.snapshots.jsonl" \
  --live-summary-json "$DATA_DIR/fast_directional_hedge_simple_paper.summary.json" \
  --fdh-state-json "$DATA_DIR/fast_directional_hedge_simple_paper.strategy_state.json" \
  --fdh-record-jsonl "$DATA_DIR/fast_directional_hedge_simple_paper.model_events.jsonl" \
  --fdh-entry-price-min 0.53 \
  --fdh-entry-price-max 0.60 \
  --fdh-entry-confirm-ticks 2 \
  --fdh-entry-confirm-min-interval-ms 150 \
  --fdh-base-position-size 2 \
  --fdh-min-ask-gap 0.04 \
  --fdh-max-spread 0.04 \
  --fdh-max-ask-sum 1.06 \
  --fdh-max-entry-drift 0.02 \
  --fdh-entry-max-slippage 0.02 \
  --fdh-initial-stop-pct 0.25 \
  --fdh-trailing-start-gain 0.15 \
  --fdh-break-even-buffer 0.00 \
  --fdh-trailing-drawdown-pct 0.20 \
  --fdh-stop-confirm-ticks 2 \
  --fdh-fast-move-window-ms 500 \
  --fdh-fast-move-threshold 0.05 \
  --fdh-fast-stop-confirm-ticks 1 \
  --fdh-emergency-stop-penetration 0.06 \
  --fdh-hedge-max-slippage 0.05 \
  --fdh-hedge-max-price 0.85 \
  --fdh-hedge-entry-max-seconds 150 \
  --fdh-hedge-entry-min-seconds 30 \
  --fdh-exit-max-slippage 0.03 \
  --fdh-take-profit-net-per-share 0.02 \
  --fdh-take-profit-confirm-ticks 1 \
  --fdh-max-entries-per-window 2 \
  --fdh-normal-entry-max-seconds 180 \
  --fdh-normal-entry-min-seconds 60 \
  --fdh-stop-new-entry-time 30 \
  --fdh-risk-only-time 15 \
  --fdh-fee-rate 0.07 \
  --fdh-max-book-age-seconds 0.50 \
  --env-file "$ENV_FILE" \
  --disable-telegram-commands \
  --disable-discord \
  --market-data-timeout 3 \
  --max-spot-age 5 \
  --max-price-alignment-difference 0.50 \
  --max-boundary-sample-offset-ms 1000 \
  --official-open-confirmations 2 \
  --official-open-stable-seconds 5 \
  --final-poll-seconds 300 \
  --final-poll-interval 1
