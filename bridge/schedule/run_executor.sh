#!/usr/bin/env bash
# Pre-open COMPUTE step — rate the universe and save the day's signals.
#
# This runs the expensive propagate (Grok) pre-open and writes the resulting
# signals (rating + price target per name) to a dated file. It places NOTHING:
# fractional buys are market orders that only fill in regular hours, so the
# at-open run_place.sh step does the placement with fresh capital/prices.
#
# Runs `claude -p` FROM the TradingAgents dir (Robinhood MCP is local-scoped
# there) with READ-ONLY tools and BRIDGE_ENABLED unset.
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"  # cron has a minimal PATH

TA_DIR="${TA_DIR:-$HOME/code/github.com/TauricResearch/TradingAgents}"
REPO="${REPO:-$HOME/code/github.com/rclod/robinhood-trading}"
ACCT="${BRIDGE_ACCOUNT_NUMBER:-963494976}"
# Full diversified universe rated each morning (held names are unioned in too).
# ~20 Grok runs/day — trim this list to cut cost.
WATCHLIST_CORE="${WATCHLIST_CORE:-AAPL,MSFT,NVDA,AMD,AVGO,GOOGL,META,NFLX,AMZN,TSLA,HD,JPM,V,GS,UNH,LLY,XOM,CVX,CAT,COST}"

# Export provider config (XAI_API_KEY, TRADINGAGENTS_*) so the bridge subprocess
# has it regardless of cwd. `uv --directory "$REPO"` runs from the repo where
# there's no .env, and find_dotenv(usecwd=True) won't reach TradingAgents/.env.
set -a; [ -f "$TA_DIR/.env" ] && . "$TA_DIR/.env"; set +a
LOGDIR="$HOME/.tradingagents/bridge/logs"
SIGDIR="$HOME/.tradingagents/bridge/signals"
mkdir -p "$LOGDIR" "$SIGDIR"
DATE="$(date +%F)"
LOG="$LOGDIR/executor-$DATE.log"
SIG="$SIGDIR/signals-$DATE.json"   # handoff to the at-open place step

# Deterministic trading-day guard (NYSE) — skip weekends/holidays before
# spending a headless agent session (and Grok on propagate).
if ! uv --directory "$REPO" run python -m bridge.market_calendar --date "$DATE" >/dev/null 2>&1; then
  echo "$(date -Is) $DATE is not a US trading day — skipping pre-open run" >> "$LOG"
  exit 0
fi

unset BRIDGE_ENABLED || true   # dry-run

read -r -d '' PROMPT <<EOF || true
You are the robinhood-trading PRE-OPEN executor (DRY-RUN, $DATE). You may ONLY
read; you have no order-placing tool. If today is not a US trading day, stop.

1. Call get_accounts; confirm the agentic_allowed account is $ACCT.
   Call get_portfolio($ACCT) and get_equity_positions($ACCT). In a temp dir write
   account.json / portfolio.json / positions.json (the respective data objects).
2. Let HELD = the position symbols. Build a watchlist = HELD ∪ {$WATCHLIST_CORE}
   as a comma-separated UPPERCASE string with no spaces.
3. Run (this calls Grok per name — may take several minutes). It also saves the
   signals (rating + price target per name) for the at-open place step:
   BRIDGE_WATCHLIST=<watchlist> uv --directory "$REPO" run python -m bridge.executor \\
     --account-number $ACCT --account-json <account.json> --portfolio-json <portfolio.json> \\
     --positions-json <positions.json> --live-ratings --save-signals "$SIG" --date $DATE
4. Report the recommendation + funding: the conviction-ranked book, what would be
   funded vs deferred, and the dry-powder reserve held back. Confirm
   execution_enabled is false and that nothing was placed (placement is the
   at-open step). Confirm the signals file was written to: $SIG
Do not place, review, or cancel any order.
EOF

cd "$TA_DIR"
{
  echo "===== pre-open run $(date -Is) ====="
  claude -p "$PROMPT" \
    --allowedTools "mcp__robinhood-trading__get_accounts mcp__robinhood-trading__get_portfolio mcp__robinhood-trading__get_equity_positions mcp__robinhood-trading__get_equity_quotes Bash Read Write" \
    --max-turns 80
  echo
} >> "$LOG" 2>&1
