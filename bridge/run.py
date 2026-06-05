"""Dry-run CLI for the bridge.

Runs the full decision pipeline end-to-end and prints the day's book — what it
*would* trade — placing nothing. This is the Phase 0 deliverable: watch the
bridge pick and size a book against real ratings before any live wiring.

Usage::

    # bundled offline demo (no network, no LLM calls, no MCP):
    python -m bridge.run --demo

    # from your own fixtures:
    python -m bridge.run --portfolio acct.json --ratings ratings.json \
                         --quotes quotes.json --date 2026-06-04

    # live ratings via propagate (slow, billable), snapshot still from file:
    python -m bridge.run --portfolio acct.json --live-ratings --date 2026-06-04

Nothing here calls place_equity_order. Execution is Phase 2 and runs as a
scheduled Claude agent with the Robinhood MCP connected.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

from rich.console import Console
from rich.table import Table

from .config import BridgeConfig
from .marketdata import get_quotes, quotes_from_fixture
from .models import OrderPlan
from .plan import build_order_plan, persist
from .sources import (
    ratings_from_fixture,
    ratings_from_propagate,
    snapshot_from_fixture,
)

_DEMO = os.path.join(os.path.dirname(__file__), "fixtures", "dry_run_demo.json")

# Respect a real terminal's width; when piped (cron/logs) fall back wide enough
# that the book table isn't column-crushed.
_width = None if sys.stdout.isatty() else int(os.getenv("COLUMNS", "150"))
console = Console(width=_width)


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _render(plan: OrderPlan, summary: dict) -> None:
    mode = "[bold green]LIVE[/]" if plan.execution_enabled else "[bold yellow]DRY-RUN[/]"
    console.print(
        f"\n[bold]Bridge book — {plan.trade_date}[/]  "
        f"equity ${plan.equity:,.0f}  ·  mode {mode}\n"
    )

    table = Table(show_lines=False, expand=False)
    for col in ("Symbol", "Rating", "Side", "Qty", "Type", "Limit", "Notional",
                "Cur→Tgt", "Sector", "Status"):
        table.add_column(col, no_wrap=True)

    for o in plan.orders:
        status = "[green]approved[/]" if o.approved else f"[red]rejected[/]: {o.reasons[0]}"
        flip = " ⟂" if o.crosses_zero else ""
        table.add_row(
            o.symbol,
            o.rating,
            o.side.upper(),
            f"{o.quantity:g}",
            o.order_type,
            f"{o.limit_price:.2f}" if o.limit_price else "—",
            f"${o.notional:,.0f}",
            f"{o.current_shares:g}→{o.target_shares:g}{flip}",
            (o.sector or "—")[:14],
            status,
        )
    console.print(table)

    if plan.holds:
        console.print(f"[dim]Holds (carried, zero delta): {', '.join(plan.holds)}[/]")
    for note in plan.notes:
        console.print(f"[yellow]• {note}[/]")

    appr = plan.approved_orders
    gross = sum(o.notional for o in appr)
    console.print(
        f"\n[bold]{len(appr)} order(s) would be placed[/] · "
        f"gross ${gross:,.0f} · {len(plan.rejected_orders)} rejected · "
        f"ledger +{summary['ledger_inserted']} rows · "
        f"day-trades today: {summary['day_trades_today']}"
    )
    console.print(f"[dim]warehouse: {summary['warehouse_file']}[/]")
    if not plan.execution_enabled:
        console.print(
            "[yellow]Nothing was placed (kill switch off). "
            "Set BRIDGE_ENABLED=1 and run via the executor agent to go live.[/]\n"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="TradingAgents → Robinhood bridge (dry-run)")
    ap.add_argument("--demo", action="store_true", help="use the bundled offline fixture")
    ap.add_argument("--portfolio", help="path to portfolio snapshot JSON")
    ap.add_argument("--ratings", help="path to {symbol: rating} JSON")
    ap.add_argument("--quotes", help="path to quotes fixture JSON")
    ap.add_argument("--live-ratings", action="store_true",
                    help="run propagate() on the watchlist (slow, billable)")
    ap.add_argument("--date", default=date.today().isoformat(), help="trade date YYYY-MM-DD")
    args = ap.parse_args()

    cfg = BridgeConfig.from_env()
    ts = datetime.now(timezone.utc).isoformat()

    if args.demo or not (args.portfolio or args.ratings):
        demo = _load(_DEMO)
        snapshot = snapshot_from_fixture(demo["portfolio"], cfg)
        ratings = ratings_from_fixture(demo["ratings"])
        quotes = quotes_from_fixture(demo["quotes"], cfg)
    else:
        snapshot = snapshot_from_fixture(_load(args.portfolio), cfg)
        if args.live_ratings:
            ratings = ratings_from_propagate(cfg.watchlist, args.date)
            quotes = get_quotes(ratings.keys(), cfg)
        else:
            ratings = ratings_from_fixture(_load(args.ratings))
            quotes = (
                quotes_from_fixture(_load(args.quotes), cfg)
                if args.quotes
                else get_quotes(ratings.keys(), cfg)
            )

    plan = build_order_plan(args.date, ratings, snapshot, quotes, cfg)
    summary = persist(plan, cfg, ts)
    _render(plan, summary)


if __name__ == "__main__":
    main()
