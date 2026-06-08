"""Tests for snapshot_from_mcp tolerance of the shapes an agent may write."""

from __future__ import annotations

from bridge.config import BridgeConfig
from bridge.sources import snapshot_from_mcp

PORTFOLIO = {"data": {"total_value": "4919.98", "buying_power": {"buying_power": "1417.98"}}}
POSITIONS = {"data": {"positions": [
    {"symbol": "NVDA", "shares_available_for_sells": "5", "average_buy_price": "211.94", "type": "long"},
]}}
ACCOUNTS_WRAPPER = {"data": {"accounts": [
    {"account_number": "573376514", "agentic_allowed": False, "type": "cash"},
    {"account_number": "963494976", "agentic_allowed": True, "type": "cash"},
]}}


def test_selects_account_by_number_from_full_wrapper():
    cfg = BridgeConfig()
    snap = snapshot_from_mcp(ACCOUNTS_WRAPPER, PORTFOLIO, POSITIONS, cfg, account_number="963494976")
    assert snap.account_number == "963494976"
    assert snap.agentic_allowed is True
    assert snap.margin_enabled is False          # cash account
    assert snap.equity == 4919.98
    assert snap.buying_power == 1417.98
    assert snap.shares_of("NVDA") == 5.0


def test_falls_back_to_agentic_account_without_number():
    cfg = BridgeConfig()
    snap = snapshot_from_mcp(ACCOUNTS_WRAPPER, PORTFOLIO, POSITIONS, cfg)
    assert snap.account_number == "963494976"  # the agentic one
    assert snap.agentic_allowed is True


def test_single_account_object_still_works():
    cfg = BridgeConfig()
    account = {"account_number": "963494976", "agentic_allowed": True, "type": "cash"}
    snap = snapshot_from_mcp(account, PORTFOLIO, POSITIONS, cfg)
    assert snap.account_number == "963494976"
    assert snap.agentic_allowed is True
