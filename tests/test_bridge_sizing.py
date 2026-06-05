"""Unit tests for bridge sizing + reconciliation."""

from __future__ import annotations

import pytest

from bridge.config import BridgeConfig
from bridge.models import MarketQuote, Position, PortfolioSnapshot
from bridge.reconcile import build_plan
from bridge.sizing import target_shares


@pytest.fixture
def cfg():
    return BridgeConfig()


def test_risk_cap_beats_wide_stop(cfg):
    # Buy tier = 15% of 25k = $3,750; but a 10% stop caps risk at 250/0.10 = $2,500.
    shares = target_shares("Buy", equity=25_000, price=100.0, stop_frac=0.10,
                           current_shares=0, cfg=cfg)
    assert shares == 25  # floor(2500/100), risk cap wins over the tier %


def test_tier_pct_beats_tight_stop(cfg):
    # With a tight 2% stop the risk cap is huge; the 15% tier % governs instead.
    shares = target_shares("Buy", equity=25_000, price=100.0, stop_frac=0.02,
                           current_shares=0, cfg=cfg)
    assert shares == 37  # floor(3750/100), tier % is the binding constraint


def test_hold_carries_current_position(cfg):
    assert target_shares("Hold", 25_000, 100.0, 0.08, current_shares=12, cfg=cfg) == 12


def test_short_target_is_negative(cfg):
    shares = target_shares("Sell", equity=25_000, price=100.0, stop_frac=0.08,
                           current_shares=0, cfg=cfg)
    assert shares < 0


def test_whole_shares_only(cfg):
    shares = target_shares("Buy", 25_000, 333.0, 0.08, current_shares=0, cfg=cfg)
    assert shares == int(shares)  # never fractional (shorting blocks fractional)


def test_reconcile_emits_delta_and_flags_flip(cfg):
    snap = PortfolioSnapshot(
        account_number="X", equity=25_000, buying_power=25_000,
        positions={"TSLA": Position("TSLA", shares=5)},
        agentic_allowed=True, margin_enabled=True,
    )
    quotes = {"TSLA": MarketQuote("TSLA", price=180.0, stop_frac=0.08,
                                  sector="Consumer Cyclical")}
    plan = build_plan("2026-06-04", {"TSLA": "Sell"}, snap, quotes, cfg)
    assert len(plan.orders) == 1
    order = plan.orders[0]
    assert order.side == "sell"
    assert order.crosses_zero is True  # long 5 -> short target
    assert order.quantity == 5 + abs(order.target_shares)


def test_hold_with_no_change_becomes_a_hold_not_an_order(cfg):
    snap = PortfolioSnapshot(
        account_number="X", equity=25_000, buying_power=25_000,
        positions={}, agentic_allowed=True, margin_enabled=True,
    )
    quotes = {"JPM": MarketQuote("JPM", price=200.0, stop_frac=0.08,
                                 sector="Financial Services")}
    plan = build_plan("2026-06-04", {"JPM": "Hold"}, snap, quotes, cfg)
    assert plan.orders == []
    assert "JPM" in plan.holds
