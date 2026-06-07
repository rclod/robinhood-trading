"""Tests for the NYSE trading-day guard."""

from __future__ import annotations

import pytest

from bridge.market_calendar import is_trading_day


@pytest.mark.parametrize("d", ["2026-06-08", "2026-06-09"])  # Mon, Tue
def test_weekdays_are_trading_days(d):
    assert is_trading_day(d) is True


@pytest.mark.parametrize("d", ["2026-06-06", "2026-06-07"])  # Sat, Sun
def test_weekends_are_not_trading_days(d):
    assert is_trading_day(d) is False


@pytest.mark.parametrize("d", ["2026-01-01", "2026-12-25"])  # New Year, Christmas
def test_holidays_are_not_trading_days(d):
    assert is_trading_day(d) is False
