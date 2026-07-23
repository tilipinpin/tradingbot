#!/bin/zsh
set -euo pipefail

readonly PROJECT_ROOT="/Users/tianliuping/.codex/worktrees/b0a7/tradingbot"
readonly PYTHON="/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
readonly RUN_TAG=${1:-$(/bin/date +%Y%m%d_%H%M%S)}

now=$(/bin/date +%s)
slug="btc-updown-5m-$((now / 300 * 300))"

cd "$PROJECT_ROOT"

export PYTHONUNBUFFERED=1
export TELEGRAM_ENABLED=false
export TELEGRAM_COMMANDS_ENABLED=false
export DISCORD_ENABLED=false
export TELEGRAM_TRADE_LEDGER="$PROJECT_ROOT/data/late_one_way_open_paper_${RUN_TAG}.events.jsonl"
export TELEGRAM_DAILY_STATE="$PROJECT_ROOT/data/late_one_way_open_paper_${RUN_TAG}.state.json"

exec "$PYTHON" -u -m src.watch_updown \
  --slug "$slug" \
  --duration 28800 \
  --interval 5 \
  --price-source POLYMARKET_CHAINLINK \
  --ws-proxy socks5h://127.0.0.1:7898 \
  --price-to-beat-proxy socks5h://127.0.0.1:7898 \
  --price-alignment-jsonl "$PROJECT_ROOT/data/late_one_way_open_paper_${RUN_TAG}.alignment.jsonl" \
  --max-price-alignment-difference 0.50 \
  --max-boundary-sample-offset-ms 1000 \
  --official-open-confirmations 2 \
  --official-open-stable-seconds 5 \
  --market-data-timeout 3 \
  --record-jsonl "$PROJECT_ROOT/data/late_one_way_open_paper_${RUN_TAG}.snapshots.jsonl" \
  --auto-trade \
  --strategy late_one_way \
  --max-spot-age 5 \
  --max-trades 2 \
  --paper-trading \
  --paper-bankroll 50 \
  --paper-stake 1 \
  --stop-when-bust \
  --one-way-entry-seconds 300 \
  --one-way-entry-cutoff-seconds 1 \
  --one-way-min-entry 0.60 \
  --one-way-max-entry 0.70 \
  --one-way-trend-samples 5 \
  --one-way-reversal-seconds 5 \
  --one-way-reversal-early-seconds 10 \
  --one-way-reversal-final-window-seconds 30 \
  --one-way-reversal-min-usd 3.00 \
  --one-way-reversal-min-bid 0.55 \
  --one-way-reversal-max-entry 0.80 \
  --one-way-reversal-min-loss-reduction-percent 0.10 \
  --one-way-reversal-min-loss-reduction-notional 0.25 \
  --max-spread 0.05 \
  --min-ask-sum 0.90 \
  --max-ask-sum 1.10 \
  --hedge-entry-cutoff-seconds 1 \
  --hedge-max-entry 0.99 \
  --hedge-max-spread 0.10 \
  --hedge-fee-rate 0.07 \
  --final-poll-seconds 30 \
  --final-poll-interval 1
