"""US equity trading-day guard (NYSE calendar).

A deterministic check the scheduling wrappers run *before* spinning up a
headless agent, so cron firings on weekends / market holidays no-op cheaply
instead of relying on the LLM to know the calendar.

Authoritative via ``pandas_market_calendars`` (a core dependency). If for some
reason the library is unavailable it degrades to a weekday-only check (skips
weekends; cannot catch holidays) and logs a warning — fail-open to "trading
day" on weekdays so a dry-run never silently skips a real session.

CLI: ``python -m bridge.market_calendar --date YYYY-MM-DD`` exits 0 on a trading
day, 1 otherwise.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date as _date
from datetime import datetime

logger = logging.getLogger(__name__)


def is_trading_day(date_str: str) -> bool:
    """True if ``date_str`` (YYYY-MM-DD) is a regular NYSE session.

    Early-close (half) days still count as trading days. Falls back to a
    weekday check if ``pandas_market_calendars`` can't be imported.
    """
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    try:
        import pandas_market_calendars as mcal

        sched = mcal.get_calendar("NYSE").schedule(start_date=date_str, end_date=date_str)
        return len(sched) > 0
    except ImportError:
        logger.warning(
            "pandas_market_calendars unavailable — weekday-only fallback "
            "(holidays not detected)"
        )
        return d.weekday() < 5
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("calendar check failed (%s) — weekday fallback", exc)
        return d.weekday() < 5


def main() -> None:
    ap = argparse.ArgumentParser(description="Exit 0 if the date is an NYSE trading day, else 1.")
    ap.add_argument("--date", default=_date.today().isoformat())
    args = ap.parse_args()
    trading = is_trading_day(args.date)
    if not trading:
        sys.stderr.write(f"{args.date} is not a US trading day\n")
    sys.exit(0 if trading else 1)


if __name__ == "__main__":
    main()
