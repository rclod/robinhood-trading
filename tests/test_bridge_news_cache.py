"""Tests for the macro-news cache + Alpha Vantage routing."""

from __future__ import annotations

from types import SimpleNamespace

from bridge.sources import _install_macro_news_cache


def test_macro_news_cache_dedupes_identical_calls():
    calls = {"n": 0}

    def fake_global_news(curr_date, look_back=None, limit=None):
        calls["n"] += 1
        return f"macro news for {curr_date}"

    iface = SimpleNamespace(VENDOR_METHODS={"get_global_news": {"yfinance": fake_global_news}})
    _install_macro_news_cache(iface)
    f = iface.VENDOR_METHODS["get_global_news"]["yfinance"]

    # Same args (as all 29 tickers would request) -> one real fetch.
    f("2026-06-09"); f("2026-06-09"); f("2026-06-09")
    assert calls["n"] == 1

    # A different date is a genuinely different fetch.
    f("2026-06-10")
    assert calls["n"] == 2


def test_macro_news_cache_is_idempotent():
    calls = {"n": 0}

    def fake(curr_date, look_back=None, limit=None):
        calls["n"] += 1
        return "x"

    iface = SimpleNamespace(VENDOR_METHODS={"get_global_news": {"yfinance": fake}})
    _install_macro_news_cache(iface)
    _install_macro_news_cache(iface)  # second install must not double-wrap
    f = iface.VENDOR_METHODS["get_global_news"]["yfinance"]
    f("2026-06-09"); f("2026-06-09")
    assert calls["n"] == 1  # still cached, not re-wrapped into a fresh cache
