"""Executor — translate a guarded OrderPlan into Robinhood MCP tool calls.

The Robinhood MCP tools are *agent-side* (a Claude session's harness), not a
Python library, so this module does NOT place orders itself. Instead it:

  1. turns each approved :class:`PlannedOrder` into the exact argument dicts for
     ``review_equity_order`` and ``place_equity_order`` (an :class:`OrderTicket`), and
  2. emits a single JSON *execution payload* the scheduled agent consumes — it
     reads the tickets and makes the MCP calls, honouring the ``place`` flag.

Key translation rules baked in here:
  - **Fractional quantities -> market order, regular_hours.** The MCP rejects
    fractional or dollar-based orders on anything but ``type=market`` +
    ``regular_hours``. Exiting a fractional position (e.g. 1.5 shares) therefore
    cannot be a marketable limit; it routes to a plain market order.
  - **Whole shares -> marketable limit** at the order's limit price (price
    protection), in the configured session.
  - Numeric fields are stringified ("5", "1.5", "204.89") as the MCP expects.
  - ``ref_id`` is the deterministic idempotency key from the plan.
  - ``place`` = plan.execution_enabled AND order.approved. When the kill switch
    is off, every ticket is ``place=false`` and the agent must place nothing.

See ``bridge/EXECUTOR.md`` for the agent runbook.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import List, Optional

from .config import BridgeConfig
from .ledger import Ledger, default_db_path
from .models import OrderPlan, PlannedOrder
from .allocate import build_rotation_plan
from .warehouse import Warehouse


def _is_fractional(qty: float) -> bool:
    return abs(qty - round(qty)) > 1e-9


def _num(x: float) -> str:
    """Compact numeric string: 5.0 -> '5', 1.5 -> '1.5'."""
    return f"{x:g}"


@dataclass
class OrderTicket:
    """MCP-ready arguments for one order, derived from a PlannedOrder."""

    planned: PlannedOrder
    review_args: dict       # args for review_equity_order
    place_args: dict        # args for place_equity_order (review_args + ref_id)
    place: bool             # may this actually be placed? (kill switch + approved)

    def to_dict(self) -> dict:
        o = self.planned
        return {
            "symbol": o.symbol,
            "place": self.place,
            "rationale": {
                "rating": o.rating,
                "current_shares": o.current_shares,
                "target_shares": o.target_shares,
                "crosses_zero": o.crosses_zero,
                "notional": round(o.notional, 2),
            },
            "review_args": self.review_args,
            "place_args": self.place_args,
        }


def build_ticket(
    order: PlannedOrder,
    account_number: str,
    cfg: BridgeConfig,
    execution_enabled: bool,
) -> OrderTicket:
    """Translate one approved PlannedOrder into review/place argument dicts."""
    # Fractional/dollar-based buy: place by USD notional, market, regular hours.
    if order.dollar_amount is not None:
        review_args = {
            "account_number": account_number,
            "symbol": order.symbol,
            "side": order.side,
            "type": "market",
            "dollar_amount": f"{order.dollar_amount:.2f}",
            "time_in_force": cfg.time_in_force,
            "market_hours": "regular_hours",
        }
        place_args = dict(review_args, ref_id=order.ref_id)
        return OrderTicket(planned=order, review_args=review_args,
                           place_args=place_args,
                           place=execution_enabled and order.approved)

    fractional = _is_fractional(order.quantity)

    if fractional:
        # Fractional can only be a market order in regular hours.
        order_type = "market"
        limit_price: Optional[str] = None
        market_hours = "regular_hours"
    else:
        order_type = order.order_type  # "limit" (marketable) by default
        limit_price = f"{order.limit_price:.2f}" if order.limit_price else None
        market_hours = cfg.market_hours

    review_args = {
        "account_number": account_number,
        "symbol": order.symbol,
        "side": order.side,
        "type": order_type,
        "quantity": _num(order.quantity),
        "time_in_force": cfg.time_in_force,
        "market_hours": market_hours,
    }
    if limit_price is not None:
        review_args["limit_price"] = limit_price

    place_args = dict(review_args)
    place_args["ref_id"] = order.ref_id  # idempotency key

    return OrderTicket(
        planned=order,
        review_args=review_args,
        place_args=place_args,
        place=execution_enabled and order.approved,
    )


def build_execution_payload(
    plan: OrderPlan,
    account_number: str,
    cfg: BridgeConfig,
) -> dict:
    """Assemble the JSON payload the scheduled agent consumes.

    Tickets are ordered sells-first so the agent frees cash (and position slots)
    before spending buying power on buys.
    """
    from .intraday import is_risk_off  # local import to avoid an import cycle

    risk_off = is_risk_off(cfg, plan.trade_date)
    ordered = sorted(plan.approved_orders, key=lambda o: o.side != "sell")
    tickets = []
    for o in ordered:
        t = build_ticket(o, account_number, cfg, plan.execution_enabled).to_dict()
        # A risk-off session halts NEW buys; de-risk sells still flow.
        if risk_off and o.side == "buy":
            t["place"] = False
            t["rationale"]["halted_by"] = "risk_off"
        tickets.append(t)
    return {
        "trade_date": plan.trade_date,
        "account_number": account_number,
        "execution_enabled": plan.execution_enabled,
        "risk_off": risk_off,
        "equity": plan.equity,
        "tickets": tickets,
        "rejected": [
            {"symbol": o.symbol, "side": o.side, "reasons": o.reasons}
            for o in plan.rejected_orders
        ],
        "holds": plan.holds,
        "assessments": plan.assessments,
        "rotation": plan.rotation,
        "notes": plan.notes,
    }


def record_placement(
    cfg: BridgeConfig,
    order: PlannedOrder,
    trade_date: str,
    status: str,
    ts: str,
    broker_order_id: Optional[str] = None,
    alerts: Optional[list] = None,
) -> None:
    """Persist an execution result: advance ledger status + append a fill row.

    The agent calls this after each place_equity_order so the ledger reflects
    reality (placed/filled/failed) and the warehouse keeps a fills history.
    """
    ledger = Ledger(default_db_path(cfg.state_dir))
    ledger.update_status(order.ref_id, status)
    Warehouse(cfg.state_dir).append_fill(
        trade_date,
        {
            "ts": ts,
            "ref_id": order.ref_id,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "status": status,
            "broker_order_id": broker_order_id,
            "alerts": alerts or [],
        },
    )


# --- CLI: emit the execution payload for the agent to act on ---------------


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    from .marketdata import get_quotes, quotes_from_fixture
    from .sources import (
        ratings_from_fixture,
        ratings_from_propagate,
        snapshot_from_fixture,
        snapshot_from_mcp,
    )

    ap = argparse.ArgumentParser(
        description="Emit the Robinhood execution payload (tickets) for an OrderPlan."
    )
    ap.add_argument("--account-number", required=True, help="agentic account number")
    # Snapshot: either our fixture format, or raw MCP json payloads.
    ap.add_argument("--portfolio", help="snapshot in fixture format")
    ap.add_argument("--account-json", help="raw MCP get_accounts account dict")
    ap.add_argument("--portfolio-json", help="raw MCP get_portfolio data dict")
    ap.add_argument("--positions-json", help="raw MCP get_equity_positions data dict")
    # Ratings + quotes.
    ap.add_argument("--ratings", help="{symbol: rating} JSON")
    ap.add_argument("--live-ratings", action="store_true", help="run propagate on the watchlist")
    ap.add_argument("--quotes", help="quotes fixture JSON (else live yfinance)")
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    cfg = BridgeConfig.from_env()

    if args.portfolio:
        snapshot = snapshot_from_fixture(_load(args.portfolio), cfg)
    elif args.account_json and args.portfolio_json and args.positions_json:
        snapshot = snapshot_from_mcp(
            _load(args.account_json), _load(args.portfolio_json),
            _load(args.positions_json), cfg, account_number=args.account_number,
        )
    else:
        ap.error("provide --portfolio OR all of --account-json/--portfolio-json/--positions-json")

    if args.live_ratings:
        ratings = ratings_from_propagate(cfg.watchlist, args.date)
    elif args.ratings:
        ratings = ratings_from_fixture(_load(args.ratings))
    else:
        ap.error("provide --ratings or --live-ratings")

    quotes = (
        quotes_from_fixture(_load(args.quotes), cfg)
        if args.quotes
        else get_quotes(ratings.keys(), cfg)
    )

    plan = build_rotation_plan(args.date, ratings, snapshot, quotes, cfg)
    # Persist the decision (idempotent ledger + warehouse) just like the dry-run.
    ts = datetime.now(timezone.utc).isoformat()
    Ledger(default_db_path(cfg.state_dir)).record_plan(plan, ts)
    Warehouse(cfg.state_dir).append_plan(plan, ts)

    payload = build_execution_payload(plan, args.account_number, cfg)
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
