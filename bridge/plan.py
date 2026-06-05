"""Orchestrator: ratings + snapshot + quotes -> guarded, persisted OrderPlan.

This is the single entry point the executor (or the dry-run CLI) calls. It runs
the pure decision pipeline (reconcile -> guards), then persists the result to
both stores (SQLite ledger for idempotency, DuckDB-queryable JSONL warehouse).
It never places an order — execution is the caller's job, and only when
``plan.execution_enabled`` is True.
"""

from __future__ import annotations

from typing import Dict

from .config import BridgeConfig
from .guards import apply_guards
from .ledger import Ledger, default_db_path
from .models import MarketQuote, OrderPlan, PortfolioSnapshot
from .reconcile import build_plan
from .warehouse import Warehouse


def build_order_plan(
    trade_date: str,
    ratings: Dict[str, str],
    snapshot: PortfolioSnapshot,
    quotes: Dict[str, MarketQuote],
    cfg: BridgeConfig,
) -> OrderPlan:
    """Run reconcile + guards. Pure: no I/O, no persistence."""
    plan = build_plan(trade_date, ratings, snapshot, quotes, cfg)
    return apply_guards(plan, snapshot, cfg)


def persist(plan: OrderPlan, cfg: BridgeConfig, ts: str) -> dict:
    """Write the plan to the ledger + warehouse. Returns a small summary."""
    ledger = Ledger(default_db_path(cfg.state_dir))
    warehouse = Warehouse(cfg.state_dir)
    inserted = ledger.record_plan(plan, ts)
    path = warehouse.append_plan(plan, ts)
    return {
        "ledger_inserted": inserted,
        "warehouse_file": path,
        "day_trades_today": ledger.day_trade_count(plan.trade_date),
    }
