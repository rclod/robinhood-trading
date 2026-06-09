"""Red-folder (high-impact economic event) timing gate.

The full analysis must run AFTER high-impact events resolve, never before (stale
analysis deployed into the reaction). This gate reads a manual weekly calendar
and tells the scheduling wrappers whether it's time to run yet.

Calendar JSON (default ``~/.tradingagents/bridge/red_folder.json``):
    {
      "2026-06-10": [{"time_ct": "07:30", "event": "Core CPI", "impact": "high"}],
      "2026-06-11": [{"time_ct": "07:30", "event": "Core PPI", "impact": "high"}]
    }

Logic:
- ``ready``: now >= max(base_time, latest *morning* high-impact event + buffer).
  So a normal day is ready at the base time; a 07:30 CPI day isn't ready until
  ~08:15 (07:30 + 45m buffer). Combined with the run-once marker (the signals
  file), the wrapper fires once at the right time.
- ``hold``: there's an *afternoon* high-impact event today that hasn't passed —
  the morning deploy should hold extra dry powder and a post-event re-rate
  deploys later.

CLI: ``python -m bridge.event_gate --check {ready|hold} [--base HH:MM] [--buffer N]``
exits 0 (yes) / 1 (no). ``--info`` prints today's events and the earliest run time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date as _date
from datetime import datetime
from typing import List, Optional

DEFAULT_CALENDAR = os.path.expanduser("~/.tradingagents/bridge/red_folder.json")
MORNING_CUTOFF_MIN = 12 * 60  # events before noon CT are "morning"
DEFAULT_BUFFER_MIN = 45        # wait this long after a morning event before running


def _to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def load_calendar(path: str = DEFAULT_CALENDAR) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _high_impact_today(calendar: dict, date_str: str) -> List[dict]:
    return [e for e in calendar.get(date_str, []) if str(e.get("impact", "")).lower() == "high"]


def earliest_run_min(calendar: dict, date_str: str, base_min: int, buffer_min: int) -> int:
    """Earliest minute-of-day the full run may start today."""
    morning = [_to_min(e["time_ct"]) for e in _high_impact_today(calendar, date_str)
               if _to_min(e["time_ct"]) < MORNING_CUTOFF_MIN]
    gate = max(morning) + buffer_min if morning else 0
    return max(base_min, gate)


def is_ready(calendar: dict, date_str: str, now_min: int, base_min: int, buffer_min: int) -> bool:
    return now_min >= earliest_run_min(calendar, date_str, base_min, buffer_min)


def pending_afternoon_event(calendar: dict, date_str: str, now_min: int) -> Optional[dict]:
    """A high-impact afternoon event today that hasn't happened yet (→ hold dry powder)."""
    for e in _high_impact_today(calendar, date_str):
        t = _to_min(e["time_ct"])
        if t >= MORNING_CUTOFF_MIN and now_min < t:
            return e
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Red-folder event timing gate.")
    ap.add_argument("--check", choices=["ready", "hold"], help="exit 0 if true, 1 if false")
    ap.add_argument("--info", action="store_true")
    ap.add_argument("--date", default=_date.today().isoformat())
    ap.add_argument("--now", help="HH:MM local (CT); default = now")
    ap.add_argument("--base", default="07:30", help="earliest base run time HH:MM")
    ap.add_argument("--buffer", type=int, default=DEFAULT_BUFFER_MIN)
    ap.add_argument("--calendar", default=DEFAULT_CALENDAR)
    args = ap.parse_args()

    cal = load_calendar(args.calendar)
    now_min = _to_min(args.now) if args.now else (datetime.now().hour * 60 + datetime.now().minute)
    base_min = _to_min(args.base)

    if args.info or not args.check:
        events = cal.get(args.date, [])
        earliest = earliest_run_min(cal, args.date, base_min, args.buffer)
        sys.stderr.write(f"{args.date} high-impact events: {[e['event']+' @'+e['time_ct'] for e in events] or 'none'}\n")
        sys.stderr.write(f"earliest full-run time: {earliest//60:02d}:{earliest%60:02d} CT "
                         f"(base {args.base}, buffer {args.buffer}m)\n")
        hold = pending_afternoon_event(cal, args.date, now_min)
        if hold:
            sys.stderr.write(f"afternoon event pending: {hold['event']} @{hold['time_ct']} → hold dry powder\n")
        return

    if args.check == "ready":
        ok = is_ready(cal, args.date, now_min, base_min, args.buffer)
        if not ok:
            er = earliest_run_min(cal, args.date, base_min, args.buffer)
            sys.stderr.write(f"not ready: waiting until {er//60:02d}:{er%60:02d} CT (red-folder event buffer)\n")
        sys.exit(0 if ok else 1)
    else:  # hold
        ev = pending_afternoon_event(cal, args.date, now_min)
        sys.exit(0 if ev else 1)


if __name__ == "__main__":
    main()
