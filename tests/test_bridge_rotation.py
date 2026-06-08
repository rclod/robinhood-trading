"""Tests for the recommendation + funding (rotation) layers."""

from __future__ import annotations

from bridge.allocate import build_rotation_plan
from bridge.config import BridgeConfig
from bridge.models import MarketQuote, Position, PortfolioSnapshot
from bridge.recommend import build_recommendation


def _snap(buying_power, positions=None, equity=25_000, margin=True):
    return PortfolioSnapshot(
        account_number="X", equity=equity, buying_power=buying_power,
        positions=positions or {}, agentic_allowed=True, margin_enabled=margin,
    )


def _q(price, sector="Technology"):
    return MarketQuote("X", price=price, stop_frac=0.08, sector=sector, ask=price)


# --- recommendation layer (capital-agnostic) -------------------------------

def test_recommendation_is_capital_agnostic_and_price_blind():
    cfg = BridgeConfig()
    snap = _snap(buying_power=0)  # no capital at all
    # A $90 and a $900 name, both Overweight -> same target weight (8% = $2,000).
    quotes = {"CHEAP": _q(90), "PRICEY": _q(900, "Energy")}
    rec = build_recommendation("d", {"CHEAP": "Overweight", "PRICEY": "Overweight"}, snap, quotes, cfg)
    tn = {t.symbol: t.target_notional for t in rec.targets}
    assert tn["CHEAP"] == tn["PRICEY"]            # price does not affect the target
    assert all(t.action == "add" for t in rec.targets)  # recommended regardless of $0 capital


# --- funding layer ----------------------------------------------------------

def test_dry_powder_reserve_is_held_back():
    cfg = BridgeConfig(cash_reserve_frac=0.10)
    snap = _snap(buying_power=1_000)
    plan = build_rotation_plan("d", {"AAPL": "Buy"}, snap, _quotes(["AAPL"]), cfg)
    r = plan.rotation
    assert r["reserve"] == 100.0                  # 10% of $1,000 BP
    assert r["deployed"] <= 900.0 + 1e-6          # never spends the reserve
    assert r["dry_powder"] >= 100.0 - 1e-6


def test_buys_are_fractional_dollar_orders():
    cfg = BridgeConfig()
    snap = _snap(buying_power=1_000)
    plan = build_rotation_plan("d", {"AAPL": "Overweight"}, snap, _quotes(["AAPL"]), cfg)
    o = plan.approved_orders[0]
    assert o.dollar_amount is not None and o.order_type == "market"
    assert o.dollar_amount <= 900.0 + 1e-6


def test_expensive_name_is_not_excluded():
    # The bug we're fixing: a pricey Overweight must still be fundable (fractional).
    cfg = BridgeConfig()
    snap = _snap(buying_power=2_000)
    quotes = {"COST": _q(900)}
    plan = build_rotation_plan("d", {"COST": "Overweight"}, snap, quotes, cfg)
    funded = [c for c in plan.rotation["candidates"] if c["status"] in ("funded", "scaled")]
    assert funded and funded[0]["symbol"] == "COST"   # funded despite $900/share


def test_conviction_priority_funds_buy_before_overweight():
    cfg = BridgeConfig()
    snap = _snap(buying_power=1_000)  # only enough for one name's full slice
    quotes = {"AAPL": _q(100), "MSFT": _q(100, "Energy")}
    plan = build_rotation_plan("d", {"AAPL": "Overweight", "MSFT": "Buy"}, snap, quotes, cfg)
    funded = [c for c in plan.rotation["candidates"] if c["status"] in ("funded", "scaled")]
    assert funded[0]["symbol"] == "MSFT"          # Buy beats Overweight, not cheapness


def test_reductions_execute_regardless_of_budget():
    cfg = BridgeConfig()
    snap = _snap(buying_power=0, positions={"QNT": Position("QNT", shares=30)}, margin=False)
    plan = build_rotation_plan("d", {"QNT": "Sell"}, snap, _quotes(["QNT"]), cfg)
    assert len(plan.approved_orders) == 1 and plan.approved_orders[0].side == "sell"


def test_assessments_capture_all_ratings_including_holds():
    cfg = BridgeConfig()
    snap = _snap(buying_power=0, positions={"AMD": Position("AMD", shares=2)})
    plan = build_rotation_plan("d", {"AMD": "Hold", "JPM": "Hold"}, snap, _quotes(["AMD", "JPM"]), cfg)
    assert plan.assessments == {"AMD": "Hold", "JPM": "Hold"}


def _quotes(syms):
    return {s: _q(100) for s in syms}
