#!/usr/bin/env bash
# Intraday risk monitor — headless dry-run tick (midday + hourly).
#
# Runs a headless `claude -p` agent FROM the TradingAgents project dir, because
# the Robinhood MCP is local-scoped to that project (see SCHEDULE.md). Tools are
# restricted to READ-ONLY Robinhood calls — there is no place_* tool available,
# so this physically cannot trade, on top of BRIDGE_ENABLED being unset.
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"  # cron has a minimal PATH

TA_DIR="${TA_DIR:-$HOME/code/github.com/TauricResearch/TradingAgents}"
REPO="${REPO:-$HOME/code/github.com/rclod/robinhood-trading}"
ACCT="${BRIDGE_ACCOUNT_NUMBER:-963494976}"

# Export provider config (XAI_API_KEY, TRADINGAGENTS_*) so the bridge subprocess
# has it regardless of cwd — needed when --rerate runs propagate. See run_executor.sh.
set -a; [ -f "$TA_DIR/.env" ] && . "$TA_DIR/.env"; set +a

LOGDIR="$HOME/.tradingagents/bridge/logs"
mkdir -p "$LOGDIR"
DATE="$(date +%F)"
LOG="$LOGDIR/intraday-$DATE.log"

# Deterministic trading-day guard (NYSE) — skip weekends/holidays cheaply,
# before spending a headless agent session.
if ! uv --directory "$REPO" run python -m bridge.market_calendar --date "$DATE" >/dev/null 2>&1; then
  echo "$(date -Is) $DATE is not a US trading day — skipping intraday tick" >> "$LOG"
  exit 0
fi

# Dry-run: kill switch stays OFF. Soft triggers stay alert-only (no --rerate) to
# avoid Grok spend on every tick; flip ADD_RERATE=1 to escalate.
unset BRIDGE_ENABLED || true
RERATE_FLAG=""
[ "${ADD_RERATE:-0}" = "1" ] && RERATE_FLAG="--rerate"

read -r -d '' PROMPT <<EOF || true
You are the robinhood-trading INTRADAY risk monitor (DRY-RUN, $DATE). You may
ONLY read; you have no order-placing tool. If today is not a US trading day, stop.

1. Call get_accounts; confirm the agentic_allowed account is $ACCT.
   Call get_portfolio($ACCT) and get_equity_positions($ACCT).
2. Let HELD = the position symbols. In a temp dir write:
   - account.json   = the $ACCT account object from get_accounts
   - portfolio.json = the get_portfolio data object
   - positions.json = the get_equity_positions data object
   - quotes.json    = {SYM:{"price":<last_trade_price>,"prev_close":<previous session close>}} for each HELD,
                      from get_equity_quotes(HELD).
   - proxy_quotes.json = same shape for the sector ETFs:
                      get_equity_quotes(SMH,XLK,XLF,XLE,XLV,XLY,XLP,XLI,XLC,XLU,XLB,XLRE).
3. Run:
   uv --directory "$REPO" run python -m bridge.intraday \\
     --account-number $ACCT --account-json <account.json> --portfolio-json <portfolio.json> \\
     --positions-json <positions.json> --quotes <quotes.json> --proxy-quotes <proxy_quotes.json> \\
     --scan-news $RERATE_FLAG --date $DATE
4. Report the "intraday" block: portfolio_pct, risk_off, and each position's
   tier + reasons; then list any de-risk tickets (all place=false in dry-run).
   State clearly that nothing was placed.
Do not place, review, or cancel any order.
EOF

cd "$TA_DIR"
{
  echo "===== intraday tick $(date -Is) ====="
  claude -p "$PROMPT" \
    --allowedTools "mcp__robinhood-trading__get_accounts mcp__robinhood-trading__get_portfolio mcp__robinhood-trading__get_equity_positions mcp__robinhood-trading__get_equity_quotes Bash Read Write" \
    --max-turns 60
  echo
} >> "$LOG" 2>&1
