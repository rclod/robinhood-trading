# Local scheduling (dry-run)

The bridge's executor + intraday monitor run as **local cron jobs** that invoke a
headless `claude -p` agent. They are NOT remote `/schedule` routines — those run
in Anthropic's cloud and cannot reach the Robinhood MCP, your `.env`, the
editable sibling dependency, or local state.

## Why these wrappers run from the TradingAgents dir

The Robinhood MCP is **local-scoped** to the `TauricResearch/TradingAgents`
project (`~/.claude.json` → `projects/<TradingAgents path>/mcpServers`), and its
OAuth token lives in `~/.claude/.credentials.json`. A headless `claude -p` only
sees that MCP when its cwd is that project dir — so both wrappers `cd` there and
run the bridge via `uv --directory <robinhood-trading repo>`.

## Safety (dry-run)

- `BRIDGE_ENABLED` is unset → the bridge plans but never places.
- `--allowedTools` lists only **read-only** Robinhood tools (`get_accounts`,
  `get_portfolio`, `get_equity_positions`, `get_equity_quotes`). There is **no
  `place_*`/`review_*`/`cancel_*` tool available**, so the agent physically
  cannot trade even if the kill switch were on. Two independent guarantees.
- Logs: `~/.tradingagents/bridge/logs/{executor,intraday}-YYYY-MM-DD.log`.

## Install the cron jobs

Times are in the machine's local timezone (**America/Chicago**). Market hours
8:30–15:00 CT. Adjust if your machine's TZ differs.

```cron
# Pre-open executor — proposed daily book (07:30 CT, weekdays)
30 7 * * 1-5 $HOME/code/github.com/rclod/robinhood-trading/bridge/schedule/run_executor.sh

# Intraday risk monitor — hourly through the session incl. midday (08:30–14:30 CT)
30 8-14 * * 1-5 $HOME/code/github.com/rclod/robinhood-trading/bridge/schedule/run_intraday.sh
```

Install with:
```bash
( crontab -l 2>/dev/null; \
  echo "30 7 * * 1-5 $HOME/code/github.com/rclod/robinhood-trading/bridge/schedule/run_executor.sh"; \
  echo "30 8-14 * * 1-5 $HOME/code/github.com/rclod/robinhood-trading/bridge/schedule/run_intraday.sh" \
) | crontab -
```

## Caveats / future work

- **Machine must be awake** at the run times (cron does not wake a sleeping host).
- **Holiday guard:** each wrapper runs `bridge.market_calendar` (NYSE via
  `pandas_market_calendars`) and exits early on weekends/holidays before spending
  an agent session. The agent prompt's "stop if not a trading day" is now just a
  backstop.
- **OAuth longevity** — if the Robinhood MCP token expires, a run will report the
  tool as unavailable; re-auth interactively with `/mcp` then resume.
- **DST** — cron uses local wall-clock, so CT↔ET stays aligned automatically.
- **Daily Grok cost** — `run_executor.sh` rates held names + `WATCHLIST_CORE`
  (default `AAPL,JPM`) each morning. Expand/trim via that env var.

## Going live (later)

Set `BRIDGE_ENABLED=1` and add the write tools (`review_equity_order`,
`place_equity_order`) to `--allowedTools` — only after the deposit settles, the
margin decision is made, and the dry-run logs look right.
