"""Lightweight intraday news scan for held names.

Reuses TradingAgents' free ``get_news_yfinance`` (no API key) to pull recent
headlines, then flags *material adverse* news with a curated negative-keyword
filter. This is deliberately cheap and best-effort: it is a trip-wire input for
the intraday monitor, not a substitute for the pre-open News Analyst (which runs
the full graph). Everything fails open — a news error never blocks the monitor.

Upgrade path: swap the keyword heuristic for a single cheap LLM classification
(severity + direction) per flagged headline, or use Alpha Vantage news sentiment
scores when ``ALPHA_VANTAGE_API_KEY`` is set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List

logger = logging.getLogger(__name__)

# Curated material-negative terms. Kept tight to avoid false positives on
# generic "falls/drops" market-chatter headlines.
ADVERSE_KEYWORDS = (
    "cut", "cuts", "slash", "slashes", "downgrade", "downgraded", "miss", "misses",
    "guidance cut", "profit warning", "warns", "warning", "plunge", "plunges",
    "probe", "investigation", "lawsuit", "sues", "recall", "halt", "halts",
    "layoff", "layoffs", "bankruptcy", "fraud", "subpoena", "default", "guts",
    "slump", "slumps", "disappoint", "disappointing", "weak guidance", "selloff",
)


@dataclass
class NewsScan:
    symbol: str
    headlines: List[str] = field(default_factory=list)
    adverse: bool = False
    hits: List[str] = field(default_factory=list)  # which keywords matched


def _headlines_from_blob(blob: str) -> List[str]:
    """Pull '### Title (source: ...)' lines out of get_news_yfinance output."""
    out = []
    for line in blob.splitlines():
        line = line.strip()
        if line.startswith("### "):
            out.append(line[4:].strip())
    return out


def scan_symbol(symbol: str, asof_date: str, lookback_days: int = 2) -> NewsScan:
    """Scan recent headlines for one symbol; flag material-adverse ones."""
    try:
        from tradingagents.dataflows.yfinance_news import get_news_yfinance

        end = asof_date
        start = (datetime.strptime(asof_date, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        blob = get_news_yfinance(symbol, start, end)
        headlines = _headlines_from_blob(blob)
    except Exception as exc:  # pragma: no cover - network/optional
        logger.debug("news scan failed for %s: %s", symbol, exc)
        return NewsScan(symbol=symbol)

    hits = []
    for h in headlines:
        low = h.lower()
        for kw in ADVERSE_KEYWORDS:
            if kw in low and kw not in hits:
                hits.append(kw)
    return NewsScan(symbol=symbol, headlines=headlines, adverse=bool(hits), hits=hits)


def scan(symbols, asof_date: str | None = None, lookback_days: int = 2) -> Dict[str, NewsScan]:
    """Scan a set of symbols. ``asof_date`` defaults to today (UTC)."""
    asof = asof_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {s: scan_symbol(s, asof, lookback_days) for s in symbols}
