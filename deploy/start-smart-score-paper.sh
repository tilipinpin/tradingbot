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
export TELEGRAM_TRADE_LEDGER="$DATA_DIR/smart_score_paper.events.jsonl"
export TELEGRAM_DAILY_STATE="$DATA_DIR/smart_score_paper.state.json"

exec "$PYTHON" -u -m src.watch_updown \
  --slug "$slug" \
  --duration 0 \
  --interval 5 \
  --auto-trade \
  --paper-trading \
  --strategy smart_score \
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
  --price-to-beat-proxy socks5h://127.0.0.1:7898 \
  --price-alignment-jsonl "$DATA_DIR/smart_score_paper.alignment.jsonl" \
  --record-jsonl "$DATA_DIR/smart_score_paper.snapshots.jsonl" \
  --live-summary-json "$DATA_DIR/smart_score_paper.summary.json" \
  --env-file "$ENV_FILE" \
  --disable-telegram-commands \
  --disable-discord \
  --market-data-timeout 3 \
  --max-spot-age 5 \
  --max-price-alignment-difference 0.50 \
  --max-boundary-sample-offset-ms 1000 \
  --official-open-confirmations 2 \
  --official-open-stable-seconds 5 \
  --signal-confirmations 2 \
  --smart-score-threshold 70 \
  --smart-score-entry-seconds 100 \
  --smart-score-cutoff-seconds 25 \
  --smart-score-min-probability 0.52 \
  --smart-score-fee-rate 0.07 \
  --smart-score-slippage 0.01 \
  --smart-score-trend-samples 3 \
  --smart-score-stability-samples 3 \
  --probability-shrinkage 1.00 \
  --edge 0.02 \
  --min-entry 0.50 \
  --max-entry 0.82 \
  --max-spread 0.05 \
  --min-ask-sum 0.90 \
  --max-ask-sum 1.10 \
  --final-poll-seconds 30 \
  --final-poll-interval 1
