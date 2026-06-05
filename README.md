# robinhood-trading

Turns [TradingAgents](https://github.com/TauricResearch/TradingAgents)' multi-agent
LLM ratings (`Buy / Overweight / Hold / Underweight / Sell`) into **guarded, sized,
idempotent Robinhood equity orders**.

The decision pipeline is pure Python and **execution-agnostic** — it decides and
sizes a day's book and places nothing. A separate executor (a scheduled Claude
agent with the Robinhood MCP) places the plan only when the `BRIDGE_ENABLED` kill
switch is on. See [`bridge/README.md`](bridge/README.md) for the full design.

> **Status: dry-run.** Validated end-to-end against a live account on Grok; no
> orders are placed while `BRIDGE_ENABLED` is unset.

## Layout

This repo holds **only our code**. The upstream framework is a dependency, kept in
a sibling clone so we can pull its updates without merge conflicts:

```
~/code/github.com/
├── rclod/robinhood-trading/        ← this repo
│   ├── bridge/                      decision pipeline, guards, ledger, warehouse
│   └── tests/
└── TauricResearch/TradingAgents/   ← upstream clone (the `tradingagents` dependency)
```

`pyproject.toml` depends on `tradingagents` via an editable path source
(`[tool.uv.sources]`) pointing at that sibling clone.

## Setup

```bash
# 1. Ensure the upstream clone exists next door:
git clone https://github.com/TauricResearch/TradingAgents.git \
    ../../TauricResearch/TradingAgents      # if not already present

# 2. Configure secrets:
cp .env.example .env      # add your XAI_API_KEY
```

`uv` resolves the editable dependency automatically on first `uv run`.

## Run the dry-run

```bash
# offline demo (no network, no LLM calls, no MCP):
uv run python -m bridge.run --demo

# live ratings on a watchlist (slow, billable: one propagate run per name):
BRIDGE_WATCHLIST=NVDA,AMD,AAPL \
  uv run python -m bridge.run --live-ratings \
  --portfolio ~/.tradingagents/bridge/snapshot.json --date $(date +%F)

# tests:
uv run --with pytest python -m pytest -q
```

## Pulling upstream updates

```bash
cd ../../TauricResearch/TradingAgents && git pull   # that's it
```

The editable source means the new upstream code is picked up immediately; bump
nothing here unless an upstream API the bridge imports has changed.

## Safety

Dry-run by default (`BRIDGE_ENABLED` off). Hard guards: agentic-account gate,
margin/shortable checks, per-name / sector / daily-notional caps (risk-reducing
exits bypass them), max-positions, cash-account buying-power ceiling, and
idempotent `ref_id` per `(symbol, date, side)`. See [`bridge/README.md`](bridge/README.md).
