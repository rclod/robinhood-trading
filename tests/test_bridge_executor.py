"""Unit tests for the executor's order-ticket translation."""

from __future__ import annotations

import pytest

from bridge.config import BridgeConfig
from bridge.executor import build_execution_payload, build_ticket
from bridge.models import MarketQuote, Position, PortfolioSnapshot
from bridge.plan import build_order_plan

ACCT = "963494976"


def _snap(**kw):
    base = dict(
        account_number=ACCT, equity=25_000, buying_power=25_000,
        positions={}, agentic_allowed=True, margin_enabled=True,
    )
    base.update(kw)
    return PortfolioSnapshot(**base)


def _plan(ratings, snap, cfg, quotes):
    return build_order_plan("2026-06-05", ratings, snap, quotes, cfg)


def test_whole_share_order_becomes_marketable_limit():
    cfg = BridgeConfig(execution_enabled=True)
    snap = _snap()
    quotes = {"AAPL": MarketQuote("AAPL", price=200.0, stop_frac=0.08, sector="Technology")}
    plan = _plan({"AAPL": "Buy"}, snap, cfg, quotes)
    order = plan.approved_orders[0]
    t = build_ticket(order, ACCT, cfg, plan.execution_enabled)
    assert t.review_args["type"] == "limit"
    assert "limit_price" in t.review_args
    assert t.review_args["market_hours"] == "regular_hours"
    assert t.place_args["ref_id"] == order.ref_id
    assert t.place is True


def test_fractional_exit_becomes_market_order():
    # An existing 1.5-share long getting exited -> fractional -> must be market.
    cfg = BridgeConfig(execution_enabled=True)
    snap = _snap(margin_enabled=False, positions={"AMD": Position("AMD", shares=1.5)})
    quotes = {"AMD": MarketQuote("AMD", price=466.0, stop_frac=0.08, sector="Technology")}
    plan = _plan({"AMD": "Sell"}, snap, cfg, quotes)
    order = plan.approved_orders[0]
    assert order.quantity == 1.5
    t = build_ticket(order, ACCT, cfg, plan.execution_enabled)
    assert t.review_args["type"] == "market"
    assert "limit_price" not in t.review_args
    assert t.review_args["market_hours"] == "regular_hours"
    assert t.review_args["quantity"] == "1.5"


def test_quantity_is_stringified_compactly():
    cfg = BridgeConfig(execution_enabled=True)
    snap = _snap(positions={"NVDA": Position("NVDA", shares=5)})
    quotes = {"NVDA": MarketQuote("NVDA", price=205.0, stop_frac=0.08, sector="Technology")}
    plan = _plan({"NVDA": "Sell"}, snap, cfg, quotes)
    t = build_ticket(plan.approved_orders[0], ACCT, cfg, plan.execution_enabled)
    # whole-share quantity has no trailing ".0"
    assert "." not in t.review_args["quantity"]


def test_kill_switch_off_forces_place_false():
    cfg = BridgeConfig(execution_enabled=False)  # kill switch off
    snap = _snap()
    quotes = {"AAPL": MarketQuote("AAPL", price=200.0, stop_frac=0.08, sector="Technology")}
    plan = _plan({"AAPL": "Buy"}, snap, cfg, quotes)
    t = build_ticket(plan.approved_orders[0], ACCT, cfg, plan.execution_enabled)
    assert t.place is False


def test_risk_off_halts_buys_in_payload(tmp_path):
    from bridge.intraday import set_risk_off
    cfg = BridgeConfig(execution_enabled=True, state_dir=str(tmp_path))
    set_risk_off(cfg, "2026-06-05", "test risk-off")
    snap = _snap()
    quotes = {"JPM": MarketQuote("JPM", price=200.0, stop_frac=0.08, sector="Financial Services")}
    plan = _plan({"JPM": "Buy"}, snap, cfg, quotes)
    payload = build_execution_payload(plan, ACCT, cfg)
    assert payload["risk_off"] is True
    buy = next(t for t in payload["tickets"] if t["review_args"]["side"] == "buy")
    assert buy["place"] is False  # buy halted by risk-off


def test_payload_orders_sells_before_buys():
    cfg = BridgeConfig(execution_enabled=True)
    snap = _snap(positions={"NVDA": Position("NVDA", shares=50)})  # big long to trim
    quotes = {
        "NVDA": MarketQuote("NVDA", price=205.0, stop_frac=0.08, sector="Technology"),
        "JPM": MarketQuote("JPM", price=200.0, stop_frac=0.08, sector="Financial Services"),
    }
    # NVDA Overweight on a 50-share long => trim (sell, risk-reducing); JPM Buy.
    plan = _plan({"NVDA": "Overweight", "JPM": "Buy"}, snap, cfg, quotes)
    payload = build_execution_payload(plan, ACCT, cfg)
    sides = [t["review_args"]["side"] for t in payload["tickets"]]
    assert sides[0] == "sell"  # sells first to free cash
    assert "buy" in sides
    assert payload["execution_enabled"] is True
