"""Unit tests for the bridge safety guards."""

from __future__ import annotations

import pytest

from bridge.config import BridgeConfig
from bridge.guards import apply_guards
from bridge.models import MarketQuote, Position, PortfolioSnapshot
from bridge.reconcile import build_plan


def _snap(**kw):
    base = dict(
        account_number="X", equity=25_000, buying_power=25_000,
        positions={}, agentic_allowed=True, margin_enabled=True,
    )
    base.update(kw)
    return PortfolioSnapshot(**base)


def _plan(ratings, snap, cfg, quotes=None):
    quotes = quotes or {
        s: MarketQuote(s, price=100.0, stop_frac=0.08, sector="Technology")
        for s in ratings
    }
    return apply_guards(build_plan("2026-06-04", ratings, snap, quotes, cfg), snap, cfg)


def test_non_agentic_account_blocks_everything():
    cfg = BridgeConfig()
    plan = _plan({"AAPL": "Buy"}, _snap(agentic_allowed=False), cfg)
    assert plan.approved_orders == []
    assert any("agentic" in r for o in plan.orders for r in o.reasons)


def test_cash_account_sell_exits_long_to_flat_not_short():
    cfg = BridgeConfig()
    snap = _snap(
        margin_enabled=False,
        positions={"AAPL": Position("AAPL", shares=10)},
    )
    plan = _plan({"AAPL": "Sell"}, snap, cfg)
    assert len(plan.approved_orders) == 1
    order = plan.approved_orders[0]
    assert order.side == "sell"
    assert order.target_shares == 0       # exit to flat, never short
    assert order.quantity == 10
    assert order.crosses_zero is False


def test_cash_account_sell_with_no_position_is_a_noop():
    cfg = BridgeConfig()
    plan = _plan({"AAPL": "Sell"}, _snap(margin_enabled=False), cfg)
    assert plan.orders == []               # nothing to exit, nothing to short


def test_margin_account_can_short():
    cfg = BridgeConfig()
    plan = _plan({"AAPL": "Sell"}, _snap(margin_enabled=True), cfg)
    assert len(plan.approved_orders) == 1
    assert plan.approved_orders[0].target_shares < 0


def test_not_shortable_blocks_short():
    cfg = BridgeConfig()
    quotes = {"AAPL": MarketQuote("AAPL", price=100.0, stop_frac=0.08,
                                  sector="Technology", shortable=False)}
    plan = _plan({"AAPL": "Sell"}, _snap(), cfg, quotes)
    assert plan.approved_orders == []
    assert any("shortable" in r for o in plan.orders for r in o.reasons)


def test_daily_notional_cap_rejects_weakest_first():
    # AAPL (Buy) sizes to ~$3.1k, NVDA (Overweight) to ~$2.0k. A $4k cap fits
    # the stronger Buy but not both — the weaker Overweight is rejected.
    cfg = BridgeConfig(max_daily_notional=4_000)
    plan = _plan({"AAPL": "Buy", "NVDA": "Overweight"}, _snap(), cfg)
    approved = [o.symbol for o in plan.approved_orders]
    assert "AAPL" in approved          # Buy is stronger conviction, survives
    assert "NVDA" not in approved      # Overweight rejected by the cap


def test_max_positions_caps_new_opens():
    cfg = BridgeConfig(max_positions=1)
    plan = _plan({"AAPL": "Buy", "NVDA": "Buy"}, _snap(), cfg)
    assert len(plan.approved_orders) == 1


def test_exit_bypasses_per_name_cap():
    # An oversized existing position whose notional exceeds the per-name cap must
    # still be exitable — the cap bounds risk-taking, not risk-shedding.
    cfg = BridgeConfig()  # per_name_cap 18% of equity
    snap = _snap(
        equity=5_000, margin_enabled=False,
        positions={"QNT": Position("QNT", shares=30)},  # 30 * $56 = $1,680 >> 18%*5000=$900
    )
    quotes = {"QNT": MarketQuote("QNT", price=56.0, stop_frac=0.08, sector="Technology")}
    plan = _plan({"QNT": "Sell"}, snap, cfg, quotes)
    assert len(plan.approved_orders) == 1
    o = plan.approved_orders[0]
    assert o.side == "sell" and o.target_shares == 0 and o.quantity == 30


def test_exits_bypass_sector_cap():
    cfg = BridgeConfig()
    snap = _snap(
        equity=5_000, margin_enabled=False,
        positions={"NVDA": Position("NVDA", shares=20), "AMD": Position("AMD", shares=20)},
    )
    quotes = {
        "NVDA": MarketQuote("NVDA", price=200.0, stop_frac=0.08, sector="Technology"),
        "AMD": MarketQuote("AMD", price=200.0, stop_frac=0.08, sector="Technology"),
    }
    plan = _plan({"NVDA": "Sell", "AMD": "Sell"}, snap, cfg, quotes)
    # Both exits approved despite combined notional far exceeding the sector cap.
    assert len(plan.approved_orders) == 2
    assert all(o.target_shares == 0 for o in plan.approved_orders)


def test_kill_switch_off_marks_dry_run():
    cfg = BridgeConfig(execution_enabled=False)
    plan = _plan({"AAPL": "Buy"}, _snap(), cfg)
    assert plan.execution_enabled is False
    assert any("kill switch" in n for n in plan.notes)
