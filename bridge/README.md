# TradingAgents → Robinhood equity bridge

Turns the framework's 5-tier ratings (`Buy / Overweight / Hold / Underweight /
Sell`) into a **guarded, sized, idempotent equity order plan**, and — in the
live path — hands that plan to an executor that places it through the Robinhood
MCP.

> **Status: Phase 0 (dry-run).** The bridge decides and sizes a full day's book
> but **places nothing**. Live execution (Phase 2) is gated behind the
> `BRIDGE_ENABLED` kill switch and runs as a scheduled Claude agent.

## Profile

A ~$25k margin account running a **daily swing** book: pre-open run, execution
at the open, long **and** short, %-of-equity sizing **capped by the 1%-risk
rule**, fully automated behind hard caps.

## The one design constraint to know

**The Robinhood MCP tools are agent-side, not a Python library.** They live in a
Claude session's harness; standalone Python (e.g. cron) cannot call
`place_equity_order`. So the bridge is split:

- **Python (this package)** does all the deciding, sizing, guarding, ledgering,
  and warehousing — and emits an `OrderPlan`. Fully testable offline.
- **Executor (Phase 2)** = a *scheduled Claude agent* with the MCP connected
  that calls `build_order_plan()`, then places `plan.approved_orders` via the
  MCP — only when `plan.execution_enabled` is True.

## Pipeline

```
ratings ─┐
quotes  ─┼─► reconcile ─► guards ─► OrderPlan ─► ledger (SQLite, idempotent)
snapshot─┘   (target      (caps,                └─► warehouse (JSONL, DuckDB)
              vs current)   shorting,
                            kill switch)
```

| Module | Job |
|---|---|
| `config.py` | All money-risk knobs + caps; `BRIDGE_*` env overrides. `account_number` has no default. |
| `policy.py` | Rating → signed target weight (Hold = carry). |
| `sizing.py` | `min(tier% × equity, 1%-risk/stop)` then per-name cap; whole shares. |
| `reconcile.py` | Target − current → delta orders; flags long↔short flips. |
| `guards.py` | agentic gate, margin/shortable, sector/per-name/daily/max-positions caps, kill switch. |
| `marketdata.py` | yfinance price + ATR(14) + sector; fails open to a fixed stop. |
| `ledger.py` | SQLite/WAL: `ref_id` idempotency + day-trade telemetry. |
| `warehouse.py` | Append-only JSONL; DuckDB queries for research/backtest. |
| `sources.py` | Pluggable ratings (`propagate` or fixture) + portfolio snapshot. |
| `plan.py` | Orchestrator: `build_order_plan()` + `persist()`. |
| `run.py` | Dry-run CLI. |
| `executor.py` | Translate plan → Robinhood MCP `review`/`place` args (fractional→market, whole→limit); execution payload. See [`EXECUTOR.md`](EXECUTOR.md). |
| `intraday.py` | Intraday risk overlay: tiered HARD-exit / SOFT-rerate / RISK-OFF trip-wires. See [`INTRADAY.md`](INTRADAY.md). |
| `news.py` | Best-effort yfinance headline scan → material-adverse flag (intraday soft trigger). |

## Two scheduled agents (Phase 2)

The Robinhood MCP is agent-side, so execution runs as scheduled Claude agents:

- **Pre-open executor** (`EXECUTOR.md`) — once daily: ratings → plan → place the book.
- **Intraday risk monitor** (`INTRADAY.md`) — midday + hourly, **sells only**:
  real-time prices + sector-ETF proxies + news → de-risk on hard drawdown / sector
  rotation; a RISK-OFF day writes a flag the executor reads to halt new buys.

Both place nothing unless `BRIDGE_ENABLED` is on.

## Run the dry-run demo

```bash
# offline: no network, no LLM calls, no MCP — just the decision pipeline
python -m bridge.run --demo

# your own data
python -m bridge.run --portfolio acct.json --ratings ratings.json --date 2026-06-04

# live ratings (slow, billable: one propagate run per watchlist name)
python -m bridge.run --portfolio acct.json --live-ratings --date 2026-06-04
```

Optional research deps: `pip install ".[bridge]"` (DuckDB + market calendars).

## Sizing formula

```
stop%    = clamp(2 × ATR(14) / price, 5%, 12%)   # framework gives no stop
notional = sign(tier) × min(|tier%| × equity, risk_per_trade × equity / stop%)
notional = min(|notional|, per_name_cap × equity)
shares   = floor(notional / price)               # whole shares (shorts block fractional)
order    = target_shares − current_shares        # delta, not absolute
```

## Roadmap

- **Phase 0 (done):** config, guards, ledger, warehouse, decision pipeline,
  dry-run CLI. Places nothing.
- **Phase 1:** validate sizing/reconciliation against real `propagate` ratings
  on the watchlist for a stretch of days; tune tier %s and caps from the logs.
- **Phase 2:** the executor agent — wire the MCP (`get_portfolio` /
  `get_equity_positions` → snapshot; `get_equity_tradability` → shortable;
  `review_equity_order` → `place_equity_order`), scheduled pre-open via
  `/schedule` with a market-calendar guard. Flip `BRIDGE_ENABLED=1` only after
  Phase 1 logs look right.

## Guard rails that hold even fully automated

`agentic_allowed` gate · margin + shortable checks · per-name / sector / daily
notional caps · max open positions · `BRIDGE_ENABLED` kill switch (off ⇒
dry-run) · idempotent `ref_id` per `(symbol, date, side)`.

PDT is **not** a guard: the $25k / day-trade-count rule was removed effective
2026-06-04, and a $25k account never bound it. The ledger keeps a day-trade
counter as telemetry only.
