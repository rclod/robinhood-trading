#!/usr/bin/env bash
# At-open PLACE step — re-allocate against fresh capital/prices and place orders.
#
# Runs shortly after the 09:30 ET open. Loads the signals computed pre-open
# (run_executor.sh), re-fetches the account snapshot + quotes (capital and prices
# move overnight; buying power may have changed), re-runs the cheap funding layer
# (NO new Grok calls), and — when live — places the fractional dollar BUYS, which
# require regular hours. Sells/exits also place here.
#
# DRY-RUN by default: BRIDGE_ENABLED unset and only read-only tools allowed, so
# it reports what it WOULD place. Going live = set BRIDGE_ENABLED=1 and add
# review_equity_order/place_equity_order to --allowedTools (see ALLOWED_TOOLS).
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

TA_DIR="${TA_DIR:-$HOME/code/github.com/TauricResearch/TradingAgents}"
REPO="${REPO:-$HOME/code/github.com/rclod/robinhood-trading}"
ACCT="${BRIDGE_ACCOUNT_NUMBER:-963494976}"
set -a; [ -f "$TA_DIR/.env" ] && . "$TA_DIR/.env"; set +a

LOGDIR="$HOME/.tradingagents/bridge/logs"
SIGDIR="$HOME/.tradingagents/bridge/signals"
mkdir -p "$LOGDIR"
DATE="$(date +%F)"
LOG="$LOGDIR/place-$DATE.log"
SIG="$SIGDIR/signals-$DATE.json"

# Trading-day guard.
if ! uv --directory "$REPO" run python -m bridge.market_calendar --date "$DATE" >/dev/null 2>&1; then
  echo "$(date -Is) $DATE is not a US trading day — skipping place step" >> "$LOG"
  exit 0
fi
# No signals computed this morning -> nothing to place.
if [ ! -f "$SIG" ]; then
  echo "$(date -Is) no signals file ($SIG) — pre-open compute did not run; skipping" >> "$LOG"
  exit 0
fi

# Read-only by default (dry-run). To go live, set BRIDGE_ENABLED=1 in the env and
# switch ALLOWED_TOOLS to include the write tools.
READ_TOOLS="mcp__robinhood-trading__get_accounts mcp__robinhood-trading__get_portfolio mcp__robinhood-trading__get_equity_positions mcp__robinhood-trading__get_equity_quotes mcp__robinhood-trading__get_equity_tradability"
WRITE_TOOLS="mcp__robinhood-trading__review_equity_order mcp__robinhood-trading__place_equity_order"
if [ "${BRIDGE_ENABLED:-0}" = "1" ]; then
  ALLOWED_TOOLS="$READ_TOOLS $WRITE_TOOLS Bash Read Write"
  MODE="LIVE — placing real orders"
else
  ALLOWED_TOOLS="$READ_TOOLS Bash Read Write"
  MODE="DRY-RUN — reporting only, no place tool available"
fi

read -r -d '' PROMPT <<EOF || true
You are the robinhood-trading AT-OPEN place step ($MODE, $DATE). The market is open.

1. get_accounts; confirm agentic_allowed account is $ACCT. get_portfolio($ACCT) and
   get_equity_positions($ACCT) -> write account.json/portfolio.json/positions.json
   (FRESH state — buying power and prices have moved since pre-open).
2. Build the execution payload from the pre-open signals (no new Grok calls):
   uv --directory "$REPO" run python -m bridge.executor \\
     --account-number $ACCT --account-json <account.json> --portfolio-json <portfolio.json> \\
     --positions-json <positions.json> --signals "$SIG" --date $DATE
3. The payload's tickets are sells-first, then conviction-ranked fractional dollar
   BUYS. Fractional buys are market orders and need regular hours — that's why this
   runs at the open.
4. If execution_enabled is false (dry-run): report what WOULD be placed (each
   ticket's symbol/side/amount, plus the funding summary) and STOP — place nothing.
   If execution_enabled is true (live): for each ticket with place=true, in order:
   skip if its ref_id already shows placed in the ledger; review_equity_order; if a
   blocking alert (insufficient buying power, halted), skip + record; else
   place_equity_order; then record_placement(...). Honour all alerts; never force.
5. Report placed / skipped / deferred with the conviction rationale.
NEVER place on a non-agentic account. NEVER place when execution_enabled is false.
EOF

cd "$TA_DIR"
{
  echo "===== at-open place $(date -Is) — $MODE ====="
  claude -p "$PROMPT" --allowedTools "$ALLOWED_TOOLS" --max-turns 80
  echo
} >> "$LOG" 2>&1
