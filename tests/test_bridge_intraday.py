"""Unit tests for the intraday risk monitor."""

from __future__ import annotations

import pytest

from bridge.config import BridgeConfig
from bridge.intraday import assess, build_intraday_plan, is_risk_off, set_risk_off
from bridge.models import MarketQuote, Position, PortfolioSnapshot

DATE = "2026-06-05"


def _snap(positions, equity=5_000, margin=False):
    return PortfolioSnapshot(
        account_number="963494976", equity=equity, buying_power=equity,
        positions=positions, agentic_allowed=True, margin_enabled=margin,
    )


def _q(price, prev, sector="Technology"):
    return MarketQuote("X", price=price, stop_frac=0.08, sector=sector, prev_close=prev)


def test_semi_proxy_overrides_broad_sector():
    cfg = BridgeConfig()
    assert cfg.sector_proxy_for("NVDA", "Technology") == "SMH"
    assert cfg.sector_proxy_for("MSFT", "Technology") == "XLK"
    assert cfg.sector_proxy_for("JPM", "Financial Services") == "XLF"


def test_hard_stop_auto_exits():
    cfg = BridgeConfig(execution_enabled=True)
    snap = _snap({"NVDA": Position("NVDA", shares=10)})
    quotes = {"NVDA": MarketQuote("NVDA", price=91.0, stop_frac=0.08,
                                  sector="Technology", prev_close=100.0)}  # -9% day
    proxy = {"SMH": MarketQuote("SMH", price=95.0, stop_frac=0.08, prev_close=100.0)}
    a = assess(DATE, snap, quotes, proxy, {}, cfg)
    nvda = a.positions[0]
    assert nvda.tier == "hard"
    plan = build_intraday_plan(DATE, a, snap, quotes, cfg)  # no rerate needed for hard
    assert len(plan.approved_orders) == 1
    o = plan.approved_orders[0]
    assert o.side == "sell" and o.target_shares == 0 and o.quantity == 10


def test_soft_sector_trigger_rerates_or_alerts():
    cfg = BridgeConfig(execution_enabled=True)
    snap = _snap({"NVDA": Position("NVDA", shares=10)})
    quotes = {"NVDA": MarketQuote("NVDA", price=96.0, stop_frac=0.08,
                                  sector="Technology", prev_close=100.0)}  # -4%, not hard
    proxy = {"SMH": MarketQuote("SMH", price=94.0, stop_frac=0.08, prev_close=100.0)}  # -6%
    a = assess(DATE, snap, quotes, proxy, {}, cfg)
    assert a.positions[0].tier == "soft"

    # No rerate fn => alert only, no order.
    alert_plan = build_intraday_plan(DATE, a, snap, quotes, cfg, rerate_fn=None)
    assert alert_plan.orders == []
    assert any("alert only" in n for n in alert_plan.notes)

    # With a bearish rerate (cash account => exit to flat) => de-risk sell.
    plan = build_intraday_plan(DATE, a, snap, quotes, cfg, rerate_fn=lambda s: "Sell")
    assert len(plan.approved_orders) == 1
    assert plan.approved_orders[0].target_shares == 0


def test_portfolio_riskoff_escalates_all_and_flags(tmp_path):
    cfg = BridgeConfig(state_dir=str(tmp_path))
    snap = _snap({"NVDA": Position("NVDA", shares=10), "AMD": Position("AMD", shares=10)})
    quotes = {
        "NVDA": MarketQuote("NVDA", price=94.0, stop_frac=0.08, sector="Technology", prev_close=100.0),
        "AMD": MarketQuote("AMD", price=94.0, stop_frac=0.08, sector="Technology", prev_close=100.0),
    }  # both -6% => book -6% <= -5%
    a = assess(DATE, snap, quotes, {}, {}, cfg)
    assert a.risk_off is True
    assert all(p.tier in ("soft", "hard") for p in a.positions)  # all escalated

    set_risk_off(cfg, DATE, a.alerts[0])
    assert is_risk_off(cfg, DATE) is True


def test_adverse_news_is_soft_trigger():
    cfg = BridgeConfig()
    snap = _snap({"AVGO": Position("AVGO", shares=5)})
    quotes = {"AVGO": MarketQuote("AVGO", price=99.0, stop_frac=0.08,
                                  sector="Technology", prev_close=100.0)}  # -1%, not hard

    class _News:  # mimic bridge.news.NewsScan
        adverse = True
        hits = ["guidance cut"]

    a = assess(DATE, snap, quotes, {}, {"AVGO": _News()}, cfg)
    assert a.positions[0].tier == "soft"
    assert any("adverse news" in r for r in a.positions[0].reasons)
