"""Tests for position-aware decision-layer context injection."""

from __future__ import annotations

import json

from bridge.position_context import install, load_holdings, position_note


def test_position_note_has_shares_cost_and_pnl():
    n = position_note("SMH", 2.105194, 569.05, 591.0)
    assert "SMH" in n and "569.05" in n
    assert "+3.9%" in n            # (591/569.05 - 1) ~ +3.86%
    assert "ADD, HOLD, TRIM, or EXIT" in n


def test_install_appends_only_for_held_names():
    class _ML:
        def get_past_context(self, ticker, *a, **k):
            return f"history for {ticker}"

    class _G:
        memory_log = _ML()

    g = _G()
    # price supplied so no network is needed
    install(g, {"SMH": {"shares": 2.1, "avg_cost": 569.05, "price": 591.0}})

    held = g.memory_log.get_past_context("SMH")
    assert "history for SMH" in held and "ALREADY HOLD" in held  # base preserved + note
    unheld = g.memory_log.get_past_context("AAPL")
    assert "ALREADY HOLD" not in unheld                          # objective for non-held


def test_install_is_idempotent():
    class _ML:
        def get_past_context(self, ticker, *a, **k):
            return "x"

    class _G:
        memory_log = _ML()

    g = _G()
    install(g, {"SMH": {"shares": 1, "avg_cost": 100, "price": 110}})
    first = g.memory_log.get_past_context
    install(g, {"SMH": {"shares": 1, "avg_cost": 100, "price": 110}})
    assert g.memory_log.get_past_context is first  # no double-wrap


def test_load_holdings_filters_zero_shares(tmp_path):
    p = tmp_path / "holdings.json"
    p.write_text(json.dumps({"SMH": {"shares": 2, "avg_cost": 569}, "OLD": {"shares": 0, "avg_cost": 1}}))
    h = load_holdings(str(p))
    assert "SMH" in h and "OLD" not in h
