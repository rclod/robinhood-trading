#!/usr/bin/env bash
# Weekly speculative scanner — Monday pre-open.
#
# Grok 4.3 with xAI live search (web + X/social) surfaces social-arbitrage
# 'lottery' candidates for the week -> speculative.json. Foreground, no MCP.
# Runs before the Monday compute so the candidates feed the dynamic watchlist.
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

TA_DIR="${TA_DIR:-$HOME/code/github.com/TauricResearch/TradingAgents}"
REPO="${REPO:-$HOME/code/github.com/rclod/robinhood-trading}"
set -a; [ -f "$TA_DIR/.env" ] && . "$TA_DIR/.env"; set +a

LOGDIR="$HOME/.tradingagents/bridge/logs"
mkdir -p "$LOGDIR"
SIG="$HOME/.tradingagents/bridge/speculative.json"

{
  echo "===== scanner $(date -Is) ====="
  uv --directory "$REPO" run python -m bridge.scanner --save "$SIG" --n 8 --date "$(date +%F)"
  echo
} >> "$LOGDIR/scanner-$(date +%F).log" 2>&1
