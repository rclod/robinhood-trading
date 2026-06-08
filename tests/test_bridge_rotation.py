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


def test_conviction_score_orders_within_tier_by_upside():
    # Two Overweights, equal tier. The one with more upside to its price target
    # funds first — replacing the arbitrary alphabetical tiebreak.
    cfg = BridgeConfig()
    snap = _snap(buying_power=500)  # only one full slice fits
    quotes = {"ZZZ": _q(100), "AAA": _q(100, "Energy")}
    # AAA priced 100 target 105 (+5%); ZZZ priced 100 target 140 (+40%).
    price_targets = {"AAA": 105.0, "ZZZ": 140.0}
    plan = build_rotation_plan("d", {"AAA": "Overweight", "ZZZ": "Overweight"},
                               snap, quotes, cfg, price_targets)
    funded = [c for c in plan.rotation["candidates"] if c["status"] in ("funded", "scaled")]
    assert funded[0]["symbol"] == "ZZZ"   # higher upside funded first, despite Z>A


def test_implausible_price_target_is_ignored():
    from bridge.recommend import conviction_score
    base = conviction_score("Overweight", None, 100.0)         # tier midpoint
    garbage = conviction_score("Overweight", 720.0, 83.0)      # +777% — ignored
    sane = conviction_score("Overweight", 110.0, 100.0)        # +10% — applied
    assert garbage == base       # outlier falls back, doesn't dominate
    assert sane > base


def test_score_falls_back_to_tier_without_targets():
    cfg = BridgeConfig()
    rec_q = {"AAA": _q(100), "BBB": _q(100)}
    from bridge.recommend import build_recommendation
    rec = build_recommendation("d", {"AAA": "Buy", "BBB": "Overweight"},
                               _snap(buying_power=0), rec_q, cfg)
    score = {t.symbol: t.score for t in rec.targets}
    assert score["AAA"] > score["BBB"]    # Buy outranks Overweight on tier alone


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
