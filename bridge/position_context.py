"""Inject current-position + P&L context into the decision layer only.

The TradingAgents analysts stay position-agnostic (objective — no anchoring on our
cost/P&L). Only the Portfolio Manager (and managers) read ``past_context``, so we
wrap the memory log's ``get_past_context`` to append a "current holding" note for
names we hold. The PM then *manages an existing position* (add/hold/trim/exit)
rather than judging a fresh entry — without biasing the upstream analysis.

Holdings come from a persistent file (``~/.tradingagents/bridge/holdings.json``,
``{symbol: {shares, avg_cost}}``) that the place step refreshes from the MCP each
day; the compute step (no MCP) reads it. Holdings are stable overnight, so the
prior session's snapshot is a fine basis for the morning analysis.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_HOLDINGS = os.path.expanduser("~/.tradingagents/bridge/holdings.json")


def load_holdings(path: str = DEFAULT_HOLDINGS) -> Dict[str, dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k.upper(): v for k, v in raw.items() if isinstance(v, dict) and float(v.get("shares", 0) or 0) != 0}
    except Exception:
        return {}


def _live_price(symbol: str) -> Optional[float]:
    try:
        import yfinance as yf

        p = yf.Ticker(symbol).fast_info.get("last_price")
        return float(p) if p else None
    except Exception:  # pragma: no cover - network/optional
        return None


def position_note(symbol: str, shares: float, avg_cost: Optional[float], price: Optional[float]) -> str:
    """The context string appended to the PM's past_context for a held name."""
    pnl = ""
    if price and avg_cost:
        pnl = f", currently ~${price:.2f} (unrealized {(price / avg_cost - 1) * 100:+.1f}%)"
    avg = f" at ${avg_cost:.2f} avg cost" if avg_cost else ""
    return (
        "=== CURRENT PORTFOLIO POSITION (manage, don't just rate) ===\n"
        f"We ALREADY HOLD {shares:g} shares of {symbol}{avg}{pnl}. Treat this as "
        "position management: decide whether to ADD, HOLD, TRIM, or EXIT given the "
        "current thesis and our entry — not as a fresh-entry call."
    )


def install(graph, holdings: Dict[str, dict]) -> None:
    """Wrap ``graph.memory_log.get_past_context`` to append current-position context.

    Prices are fetched once here (not per call). No-op when there are no holdings.
    """
    ml = getattr(graph, "memory_log", None)
    if ml is None or not holdings:
        return
    orig = ml.get_past_context
    if getattr(orig, "_bridge_position", False):
        return

    notes: Dict[str, str] = {}
    for sym, pos in holdings.items():
        shares = float(pos.get("shares", 0) or 0)
        if shares == 0:
            continue
        avg = pos.get("avg_cost")
        price = pos.get("price") or _live_price(sym)
        notes[sym] = position_note(sym, shares, float(avg) if avg else None,
                                   float(price) if price else None)

    def wrapped(ticker, *args, **kwargs):
        base = orig(ticker, *args, **kwargs)
        note = notes.get(ticker.upper())
        if note:
            return f"{base}\n\n{note}" if base else note
        return base

    wrapped._bridge_position = True
    ml.get_past_context = wrapped
    logger.info("position context installed for %d holding(s)", len(notes))
