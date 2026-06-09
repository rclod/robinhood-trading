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
from datetime import date, datetime, timezone

from . import usage
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

    # Dynamic universe: static watchlist + HELD names (manage off-watchlist
    # holdings) + this week's speculative scanner candidates (analyse new picks).
    from .position_context import load_holdings
    from .scanner import speculative_tickers

    universe: list = []
    for s in list(cfg.watchlist) + list(load_holdings().keys()) + speculative_tickers():
        s = s.upper()
        if s not in universe:
            universe.append(s)
    logger.info("compute: %d names (%d static + held + speculative) for %s",
                len(universe), len(cfg.watchlist), args.date)

    usage.reset()
    usage.install()  # capture token usage from the propagate LLM calls
    signals = signals_from_propagate(universe, args.date)

    os.makedirs(os.path.dirname(os.path.abspath(args.save_signals)), exist_ok=True)
    with open(args.save_signals, "w", encoding="utf-8") as f:
        json.dump(signals, f, indent=2)
    rated = sum(1 for v in signals.values() if v.get("rating"))
    logger.info("saved %d/%d signals -> %s", rated, len(universe), args.save_signals)

    # Token usage + estimated cost for the run.
    rep = usage.report()
    logger.info("token usage: %d calls, %s tokens, cache-hit %.0f%%, est ~$%.2f",
                rep["total_calls"], f"{rep['total_tokens']:,}",
                rep["cache_hit_rate"] * 100, rep["total_cost_usd"])
    for r in rep["by_model"]:
        logger.info("  %s: %d calls  in=%s (cached %s) out=%s  ~$%.4f",
                    r["model"], r["calls"], f"{r['prompt']:,}", f"{r['cached']:,}",
                    f"{r['completion']:,}", r["cost_usd"])
    udir = os.path.join(cfg.state_dir, "usage")
    os.makedirs(udir, exist_ok=True)
    with open(os.path.join(udir, f"usage-{args.date}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                            "date": args.date, "names": len(universe), **rep}) + "\n")


if __name__ == "__main__":
    main()
