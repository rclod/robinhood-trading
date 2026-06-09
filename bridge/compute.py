"""Pre-open COMPUTE — run propagate on the universe and save the day's signals.

This is the reliable replacement for driving propagate through a headless
`claude -p` agent (which could time out before ~29 names finished). propagate
needs only Grok + yfinance + news sources — NOT the Robinhood MCP — so it runs
directly here in the foreground. The MCP is only needed by the place step, which
consumes ``signals-<date>.json``.

Usage:
    python -m bridge.compute --save-signals <path> [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date

from .config import BridgeConfig
from .sources import signals_from_propagate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bridge.compute")


def main() -> None:
    ap = argparse.ArgumentParser(description="Rate the universe and save signals (rating + price target).")
    ap.add_argument("--save-signals", required=True, help="output JSON path")
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    cfg = BridgeConfig.from_env()
    logger.info("compute: %d names for %s (provider via env)", len(cfg.watchlist), args.date)
    signals = signals_from_propagate(cfg.watchlist, args.date)

    os.makedirs(os.path.dirname(os.path.abspath(args.save_signals)), exist_ok=True)
    with open(args.save_signals, "w", encoding="utf-8") as f:
        json.dump(signals, f, indent=2)
    rated = sum(1 for v in signals.values() if v.get("rating"))
    logger.info("saved %d/%d signals -> %s", rated, len(cfg.watchlist), args.save_signals)


if __name__ == "__main__":
    main()
