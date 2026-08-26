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
export TELEGRAM_TRADE_LEDGER="$DATA_DIR/ewma_twap_paper.trade_events.jsonl"
export TELEGRAM_DAILY_STATE="$DATA_DIR/ewma_twap_paper.notification_state.json"

exec "$PYTHON" -u -m src.watch_updown \
  --slug "$slug" \
  --duration 0 \
  --interval 1 \
  --auto-trade \
  --paper-trading \
  --strategy ewma_twap_fair \
  --paper-bankroll 50 \
  --paper-stake 1 \
  --paper-shares 0 \
  --stop-when-bust \
  --max-trades 1 \
  --max-consecutive-losses 0 \
  --pause-windows-after-losses 0 \
  --price-source POLYMARKET_CHAINLINK \
  --crypto-resolution-mode auto \
  --ws-proxy http://127.0.0.1:7897 \
  --price-to-beat-proxy socks5h://127.0.0.1:7898 \
  --price-alignment-jsonl "$DATA_DIR/ewma_twap_paper.alignment.jsonl" \
  --record-jsonl "$DATA_DIR/ewma_twap_paper.snapshots.jsonl" \
  --live-summary-json "$DATA_DIR/ewma_twap_paper.summary.json" \
  --ewma-twap-lambda 0.94 \
  --ewma-twap-realized-seconds 60 \
  --ewma-twap-weight 0.70 \
  --ewma-twap-min-edge 0.015 \
  --ewma-twap-half-spread-buffer 0.0025 \
  --ewma-twap-slippage-buffer 0.0030 \
  --ewma-twap-fee-rate 0.07 \
  --ewma-twap-kelly-fraction 0.25 \
  --ewma-twap-kelly-bankroll 50 \
  --ewma-twap-max-notional 25 \
  --ewma-twap-entry-seconds 300 \
  --ewma-twap-cutoff-seconds 75 \
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
