"""Tests for the red-folder event timing gate."""

from __future__ import annotations

from bridge.event_gate import (
    _to_min,
    earliest_run_min,
    is_ready,
    pending_afternoon_event,
)

CAL = {
    "2026-06-10": [{"time_ct": "07:30", "event": "Core CPI", "impact": "high"}],
    "2026-06-11": [{"time_ct": "13:00", "event": "FOMC", "impact": "high"}],
    "2026-06-12": [{"time_ct": "07:30", "event": "Jobless Claims", "impact": "low"}],  # not high
}
BASE = _to_min("07:30")


def test_morning_event_delays_run():
    d = "2026-06-10"
    assert earliest_run_min(CAL, d, BASE, 45) == _to_min("08:15")  # 07:30 + 45m
    assert is_ready(CAL, d, _to_min("08:00"), BASE, 45) is False
    assert is_ready(CAL, d, _to_min("08:15"), BASE, 45) is True


def test_normal_day_ready_at_base():
    d = "2026-06-15"  # not in calendar
    assert is_ready(CAL, d, _to_min("07:15"), BASE, 45) is False  # before base
    assert is_ready(CAL, d, _to_min("07:30"), BASE, 45) is True


def test_low_impact_event_is_ignored():
    # A low-impact event must not delay the run.
    assert is_ready(CAL, "2026-06-12", _to_min("07:30"), BASE, 45) is True


def test_afternoon_event_triggers_hold_until_it_passes():
    d = "2026-06-11"  # FOMC 13:00
    assert pending_afternoon_event(CAL, d, _to_min("09:00")) is not None   # before -> hold
    assert pending_afternoon_event(CAL, d, _to_min("13:30")) is None       # after -> clear
    # A morning-only event day never triggers the afternoon hold.
    assert pending_afternoon_event(CAL, "2026-06-10", _to_min("09:00")) is None
