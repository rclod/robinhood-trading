"""Tests for rotation / capital-allocation logic."""

from __future__ import annotations

from bridge.allocate import build_rotation_plan
from bridge.config import BridgeConfig
from bridge.models import MarketQuote, Position, PortfolioSnapshot


def _snap(buying_power, positions=None, equity=25_000, margin=True):
    return PortfolioSnapshot(
        account_number="X", equity=equity, buying_power=buying_power,
        positions=positions or {}, agentic_allowed=True, margin_enabled=margin,
    )


def _q(price, sector="Technology"):
    return MarketQuote("X", price=price, stop_frac=0.08, sector=sector, ask=price)


def test_budget_limits_buys_to_settled_buying_power():
    cfg = BridgeConfig()
    # Two Buys want ~$3.7k each, but only $1,000 settled BP — only the strongest funds.
    snap = _snap(buying_power=1_000)
    quotes = {"AAPL": _q(100), "MSFT": _q(100, "Communication Services")}
    plan = build_rotation_plan("2026-06-08", {"AAPL": "Buy", "MSFT": "Buy"}, snap, quotes, cfg)
    assert plan.rotation["deployed"] <= 1_000 + 1e-6
    # capital fully (or nearly) deployed into whole shares
    assert plan.rotation["deployed"] >= 900


def test_marginal_buy_is_scaled_to_fit():
    cfg = BridgeConfig()
    snap = _snap(buying_power=550)  # 5 shares @ $100 worth fits; tier wants more
    quotes = {"AAPL": _q(100)}
    plan = build_rotation_plan("2026-06-08", {"AAPL": "Buy"}, snap, quotes, cfg)
    cand = plan.rotation["candidates"][0]
    assert cand["status"] in ("funded", "scaled")
    assert plan.rotation["deployed"] <= 550 + 1e-6
    # only whole shares
    o = plan.approved_orders[0]
    assert o.quantity == int(o.quantity)


def test_reductions_execute_regardless_of_budget():
    cfg = BridgeConfig()
    # Zero buying power, but a held long rated Sell on a cash account must still exit.
    snap = _snap(buying_power=0, positions={"QNT": Position("QNT", shares=30)}, margin=False)
    quotes = {"QNT": _q(56)}
    plan = build_rotation_plan("2026-06-08", {"QNT": "Sell"}, snap, quotes, cfg)
    assert len(plan.approved_orders) == 1
    assert plan.approved_orders[0].side == "sell"


def test_buy_ranked_strongest_first():
    cfg = BridgeConfig()
    snap = _snap(buying_power=500)  # only enough for one name
    quotes = {"AAPL": _q(100), "MSFT": _q(100, "Energy")}
    # Buy beats Overweight — AAPL(Buy) funds, MSFT(Overweight) defers.
    plan = build_rotation_plan("2026-06-08", {"AAPL": "Overweight", "MSFT": "Buy"}, snap, quotes, cfg)
    funded = [c for c in plan.rotation["candidates"] if c["status"] in ("funded", "scaled")]
    assert funded and funded[0]["symbol"] == "MSFT"  # Buy funded first


def test_assessments_capture_all_ratings_including_holds():
    cfg = BridgeConfig()
    snap = _snap(buying_power=0, positions={"AMD": Position("AMD", shares=2)})
    quotes = {"AMD": _q(100), "JPM": _q(100, "Financial Services")}
    plan = build_rotation_plan("2026-06-08", {"AMD": "Hold", "JPM": "Hold"}, snap, quotes, cfg)
    assert plan.assessments == {"AMD": "Hold", "JPM": "Hold"}  # holds keep ratings
