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

# Run-once guard: signals file is the marker. If we already computed today, stop
# (cron fires this every 15 min through the morning so the event gate can pick
# the right time; we only want one run).
if [ -f "$SIG" ]; then exit 0; fi

# Lock: compute takes 40-75 min, longer than the 15-min cron interval, so prevent
# overlapping runs. Non-blocking flock; if another instance holds it, exit.
exec 9>"$SIGDIR/compute-$DATE.lock" || exit 1
flock -n 9 || exit 0
if [ -f "$SIG" ]; then exit 0; fi  # re-check after acquiring the lock

# Red-folder event gate: don't run the full analysis until morning high-impact
# events (CPI/PPI/NFP at 07:30 CT, etc.) have passed + buffer. Base 07:30 CT.
if ! uv --directory "$REPO" run python -m bridge.event_gate --check ready --base 07:30 --date "$DATE" 2>>"$LOG"; then
  exit 0   # waiting for a red-folder event to clear; a later cron tick retries
fi

# The watchlist (single names + sector ETFs) comes from bridge config; override
# with BRIDGE_WATCHLIST to trim cost. ~29 Grok runs by default.
{
  echo "===== compute $(date -Is) ====="
  uv --directory "$REPO" run python -m bridge.compute --save-signals "$SIG" --date "$DATE"
  echo
} >> "$LOG" 2>&1
