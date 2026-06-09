"""Tests for pre-trade portfolio status + P&L."""

from __future__ import annotations

from bridge.models import MarketQuote, Position, PortfolioSnapshot
from bridge.portfolio import portfolio_status


def test_per_position_and_total_pnl():
    snap = PortfolioSnapshot(
        account_number="X", equity=10_000, buying_power=3_000,
        positions={
            "NVDA": Position("NVDA", shares=10, avg_cost=200.0),  # +10%
            "XOM": Position("XOM", shares=20, avg_cost=110.0),    # -10%
        },
        agentic_allowed=True, margin_enabled=False,
    )
    quotes = {
        "NVDA": MarketQuote("NVDA", price=220.0, stop_frac=0.08),
        "XOM": MarketQuote("XOM", price=99.0, stop_frac=0.08),
    }
    s = portfolio_status(snap, quotes, {"NVDA": "Hold", "XOM": "Underweight"})

    byp = {p["symbol"]: p for p in s["positions"]}
    assert byp["NVDA"]["unrealized_pnl"] == 200.0 and byp["NVDA"]["unrealized_pnl_pct"] == 10.0
    assert byp["XOM"]["unrealized_pnl"] == -220.0 and byp["XOM"]["unrealized_pnl_pct"] == -10.0
    assert byp["NVDA"]["rating"] == "Hold"
    # winners sorted before losers
    assert s["positions"][0]["symbol"] == "NVDA"
    # totals: cost 2000+2200=4200, mv 2200+1980=4180 -> -20
    assert s["total_unrealized_pnl"] == -20.0
    assert s["net_liq"] == 10_000 and s["buying_power"] == 3_000


def test_missing_quote_is_tolerated():
    snap = PortfolioSnapshot(
        account_number="X", equity=5_000, buying_power=5_000,
        positions={"QNT": Position("QNT", shares=5, avg_cost=50.0)},
        agentic_allowed=True, margin_enabled=False,
    )
    s = portfolio_status(snap, {}, {})  # no quote for QNT
    assert s["positions"][0]["price"] is None
    assert s["positions"][0]["unrealized_pnl"] is None
