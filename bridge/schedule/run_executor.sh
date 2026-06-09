#!/usr/bin/env bash
# Pre-open COMPUTE step — rate the universe and save the day's signals.
#
# Runs propagate (Grok) DIRECTLY in the foreground — no `claude -p`, no MCP.
# propagate needs only Grok + yfinance + news, so this can't be cut off by an
# agent session limit (the old headless version timed out on ~29 names). The
# at-open run_place.sh step pulls account state from the MCP and does placement.
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"  # cron has a minimal PATH

TA_DIR="${TA_DIR:-$HOME/code/github.com/TauricResearch/TradingAgents}"
REPO="${REPO:-$HOME/code/github.com/rclod/robinhood-trading}"

# Export provider config (XAI_API_KEY, TRADINGAGENTS_*) for the propagate run.
set -a; [ -f "$TA_DIR/.env" ] && . "$TA_DIR/.env"; set +a

LOGDIR="$HOME/.tradingagents/bridge/logs"
SIGDIR="$HOME/.tradingagents/bridge/signals"
mkdir -p "$LOGDIR" "$SIGDIR"
DATE="$(date +%F)"
LOG="$LOGDIR/executor-$DATE.log"
SIG="$SIGDIR/signals-$DATE.json"

# Deterministic trading-day guard (NYSE) — skip weekends/holidays before any Grok spend.
if ! uv --directory "$REPO" run python -m bridge.market_calendar --date "$DATE" >/dev/null 2>&1; then
  echo "$(date -Is) $DATE is not a US trading day — skipping compute" >> "$LOG"
  exit 0
fi

# The watchlist (single names + sector ETFs) comes from bridge config; override
# with BRIDGE_WATCHLIST to trim cost. ~29 Grok runs by default.
{
  echo "===== compute $(date -Is) ====="
  uv --directory "$REPO" run python -m bridge.compute --save-signals "$SIG" --date "$DATE"
  echo
} >> "$LOG" 2>&1
