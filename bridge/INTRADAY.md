# Intraday risk monitor runbook

A second scheduled Claude agent, separate from the pre-open executor. It runs
**midday + roughly hourly** through the session, checks for fast drawdown / sector
rotation / adverse news, and emits a **de-risk payload** (sells only) plus a
risk-off signal. It does NOT open new positions. Like the executor, it places
nothing unless `BRIDGE_ENABLED` is on.

Why a separate loop: the pre-open executor is blind to intraday moves (the
NVDA/AMD-into-semi-rotation problem). This loop is cheap and deterministic; it
only escalates to the expensive `propagate` re-rate when a soft trigger fires.

## Tiers (Moderate preset — see `bridge/config.py`)
- **HARD (auto-exit):** a held name down ≥ `intraday_position_stop` (8%) on the
  day → shed `intraday_hard_exit_fraction` (default: full exit).
- **SOFT (re-rate):** the name's sector ETF proxy down ≥ `intraday_sector_drop`
  (4%) — SMH for semis, else the SPDR sector ETF — OR a material adverse headline
  → run a *targeted* `propagate` on just that name and act on the fresh rating.
  (Without `--rerate`, soft triggers are alert-only.)
- **RISK-OFF:** book down ≥ `intraday_portfolio_drawdown` (5%) on the day → write
  a `risk_off-<date>.flag` (halts new buys in the executor) and escalate every
  held name to a re-rate.

## Daily sequence (each tick)

### 1. Fetch real-time state via MCP
- `get_accounts` → agentic account; `get_portfolio` + `get_equity_positions`
  → `account.json` / `portfolio.json` / `positions.json`.
- Build the symbol list: held names **+ their sector ETF proxies** (SMH, XLK,
  XLF, …). `get_equity_quotes(symbols)` returns real-time quote **and prior
  session close** for each — write `quotes.json` (held) and `proxy_quotes.json`
  (ETFs) as `{symbol: {price, prev_close, sector}}`.

### 2. Run the monitor
```bash
uv --directory <repo> run python -m bridge.intraday \
  --account-number "$BRIDGE_ACCOUNT_NUMBER" \
  --account-json account.json --portfolio-json portfolio.json \
  --positions-json positions.json \
  --quotes quotes.json --proxy-quotes proxy_quotes.json \
  --scan-news --rerate --date "$(date +%F)"
```
- `--scan-news` adds a free yfinance headline scan (material-adverse → soft).
- `--rerate` lets soft triggers escalate to a targeted Grok re-rate. Drop it to
  keep soft triggers alert-only (cheaper).

Output JSON: the standard execution payload (`tickets[]` with `place` flags,
sells-first) plus an `intraday` block (`portfolio_pct`, `risk_off`, per-position
tiers + reasons, `sector_moves`).

### 3. Act (only if `execution_enabled`)
Same as the executor: for each ticket with `place == true`, idempotency-check the
`ref_id`, `review_equity_order`, honour alerts, `place_equity_order`,
`record_placement(...)`. All de-risk orders are sells → they bypass exposure caps
and need no buying power.

### 4. Report
Print the `intraday` block (what tripped and why) + placed/skipped. If `risk_off`
is true, say so loudly — the executor will halt buys for the rest of the day.

## Hard rules
- **Sells only.** This loop never opens or adds to a position.
- Never place when `execution_enabled` is false; never bypass review alerts.
- A targeted re-rate runs `propagate` on the flagged name **only** — never the
  whole watchlist.

## Agent prompt template (for `/schedule`, midday + hourly)

```
You are the robinhood-trading intraday risk monitor. Today is {{date}}, tick {{time}}.
Repo: ~/code/github.com/rclod/robinhood-trading (run via `uv --directory <repo> run ...`).

1. If not a US trading session hour, stop.
2. get_accounts → agentic_allowed account == BRIDGE_ACCOUNT_NUMBER; get_portfolio +
   get_equity_positions → write account/portfolio/positions json.
3. Held names + their sector ETF proxies (SMH for NVDA/AMD/AVGO/…, else XLK/XLF/…):
   get_equity_quotes(all) → write quotes.json (held) and proxy_quotes.json (ETFs)
   as {symbol:{price, prev_close, sector}}.
4. Run `python -m bridge.intraday --account-number <acct> --account-json ...
   --quotes quotes.json --proxy-quotes proxy_quotes.json --scan-news --rerate --date {{date}}`.
5. Report the intraday block (tiers + reasons, portfolio_pct, risk_off).
6. If execution_enabled: place each ticket with place=true (idempotency-check,
   review, honour alerts, place, record_placement). SELLS ONLY. Never force an alert.
NEVER open/add positions here. NEVER place when execution_enabled is false.
```
