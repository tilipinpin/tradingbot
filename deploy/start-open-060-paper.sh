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
export TELEGRAM_TRADE_LEDGER="$DATA_DIR/open_060_paper.events.jsonl"
export TELEGRAM_DAILY_STATE="$DATA_DIR/open_060_paper.state.json"

exec "$PYTHON" -u -m src.watch_updown \
  --slug "$slug" \
  --duration 0 \
  --interval 1 \
  --auto-trade \
  --paper-trading \
  --strategy open_060 \
  --paper-bankroll 50 \
  --paper-shares 5 \
  --paper-stake 1 \
  --stop-when-bust \
  --order-size 5 \
  --max-trades 1 \
  --max-consecutive-losses 0 \
  --pause-windows-after-losses 0 \
  --price-source POLYMARKET_CHAINLINK \
  --ws-proxy socks5h://127.0.0.1:7898 \
  --record-jsonl "$DATA_DIR/open_060_paper.snapshots.jsonl" \
  --env-file "$ENV_FILE" \
  --disable-telegram-commands \
  --disable-discord \
  --market-data-timeout 3 \
  --max-spot-age 5 \
  --open-060-entry-seconds 300 \
  --open-060-cutoff-seconds 270 \
  --open-060-target 0.60 \
  --open-060-slippage 0.01 \
  --open-060-fee-rate 0.07 \
  --open-060-initial-ask 0.50 \
  --max-spread 0.05 \
  --min-ask-sum 0.90 \
  --max-ask-sum 1.10 \
  --final-poll-seconds 30 \
  --final-poll-interval 1
