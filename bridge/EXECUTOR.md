# Executor runbook (Phase 2)

The executor is a **scheduled Claude agent** with the Robinhood MCP connected. It
runs once per trading day, pre-open, fetches account state via MCP, runs the
bridge to produce an execution payload, and places the approved orders — **only
when `BRIDGE_ENABLED` is on**. The Python side (`bridge/executor.py`) does the
deciding and the order-argument translation; the agent does the MCP calls.

> Standalone cron CANNOT do this — the MCP tools live in the agent's harness.
> Schedule this as a Claude routine (`/schedule`), not a system cron job.

## Daily sequence

### 0. Preflight gates (abort the run if any fails)
- **Trading day?** Skip weekends / market holidays.
- **Kill switch.** Read `BRIDGE_ENABLED`. If off → run steps 1–4 and report the
  payload, then STOP. Place nothing. (The payload's tickets will all be
  `place:false`.)
- **Agentic account.** `get_accounts` → select the account with
  `agentic_allowed=true`. Confirm it equals `BRIDGE_ACCOUNT_NUMBER`. Never place
  on a non-agentic account (the MCP rejects it anyway).

### 1. Fetch live state via MCP → temp JSON files
- `get_accounts` → save the chosen account dict to `account.json`.
- `get_portfolio(account_number)` → `portfolio.json` (total_value, buying_power).
- `get_equity_positions(account_number)` → `positions.json`.
- *(optional)* `get_equity_tradability(account_number, symbols)` → confirm
  shortable / not halted for any name you may short.

### 2. Ratings
Run TradingAgents on the watchlist (slow, billable — one `propagate` per name).
This is the `--live-ratings` path; or supply a `ratings.json` from an earlier run.

### 3. Build the execution payload
```bash
uv --directory <repo> run python -m bridge.executor \
  --account-number "$BRIDGE_ACCOUNT_NUMBER" \
  --account-json account.json \
  --portfolio-json portfolio.json \
  --positions-json positions.json \
  --live-ratings --date "$(date +%F)"
```
Returns JSON: `{execution_enabled, equity, tickets[], rejected[], holds[], notes[]}`.
Each ticket carries `review_args`, `place_args` (with `ref_id`), a `place` flag,
and the `rationale`. Tickets are ordered **sells-first**.

### 4. Execute (only if `execution_enabled` is true)
For each ticket where `place == true`, in order:
1. **Idempotency check.** If the ledger already shows this `ref_id` as
   `placed`/`filled`, skip it (a retry of the same logical order).
2. `review_equity_order(**ticket.review_args)` → inspect estimated cost + alerts.
   If an alert blocks the order (insufficient buying power, instrument halted,
   etc.), **skip it** and record `status="skipped"` with the alert. Do not force.
3. `place_equity_order(**ticket.place_args)` — `ref_id` makes this idempotent;
   re-send the SAME `ref_id` only on a transient transport retry.
4. Record the result:
   ```python
   from bridge.executor import record_placement
   record_placement(cfg, order, trade_date, status="placed",
                    broker_order_id=<id>, alerts=<alerts>)
   ```

### 5. Report
Summarise placed / skipped / failed, with each order's rationale. The ledger
(status) and warehouse (`fills-YYYY-MM-DD.jsonl`) are updated for later P&L
attribution.

## Hard rules
- **Never place when `execution_enabled` is false.** Dry-run is the default.
- **Never bypass `review_equity_order`'s alerts** to force a blocked order.
- **One logical order per `(symbol, date, side)`** — the `ref_id` enforces it;
  honour the ledger idempotency check.
- **Stop the run** on repeated place failures rather than retrying blindly.
- Fractional exits route to `type=market` automatically (MCP rejects fractional
  limits) — don't "fix" them into limits.

## Agent prompt template (for `/schedule`, pre-open)

```
You are the robinhood-trading executor. Today is {{date}}.
Repo: ~/code/github.com/rclod/robinhood-trading  (run via `uv --directory <repo> run ...`).

1. PREFLIGHT: If not a US trading day, stop. Read BRIDGE_ENABLED from the repo .env.
   Call get_accounts; pick agentic_allowed=true; confirm it == BRIDGE_ACCOUNT_NUMBER.
2. FETCH: get_portfolio + get_equity_positions for that account; write account.json,
   portfolio.json, positions.json to a temp dir.
3. BUILD: run `python -m bridge.executor --account-number <acct> --account-json ...
   --portfolio-json ... --positions-json ... --live-ratings --date {{date}}`.
4. If execution_enabled is false: print the payload (the proposed book) and STOP —
   place nothing.
5. If true: for each ticket with place=true (sells first): skip if ref_id already
   placed; review_equity_order; if a blocking alert, skip+record; else
   place_equity_order; record_placement(...). Honour all alerts; never force.
6. REPORT placed/skipped/failed with rationale.
NEVER place on a non-agentic account or when execution_enabled is false.
```
