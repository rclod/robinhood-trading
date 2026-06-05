"""Per-symbol pricing for sizing and limit-price construction.

The framework supplies no price/stop, so the bridge derives them. The default
provider uses yfinance (already a TradingAgents dependency) for last price +
ATR(14) and resolves sector via the shared instrument-identity helper. Every
lookup fails open: if yfinance is unavailable or a field is missing, the quote
falls back to the configured fixed stop and conservative defaults so a dry-run
never crashes on a network hiccup.

For offline/dry-run testing, quotes can instead be loaded straight from a
fixture (see :func:`quotes_from_fixture`).
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, Optional

from .config import BridgeConfig
from .models import MarketQuote

logger = logging.getLogger(__name__)


def _atr(symbol: str, period: int = 14) -> Optional[float]:
    """ATR(14) from daily bars via yfinance; ``None`` if unavailable."""
    try:
        import yfinance as yf  # lazy: keep dry-run import-light

        hist = yf.Ticker(symbol).history(period="2mo", interval="1d")
        if hist is None or len(hist) < period + 1:
            return None
        high, low, close = hist["High"], hist["Low"], hist["Close"]
        prev_close = close.shift(1)
        tr = (high - low).combine((high - prev_close).abs(), max).combine(
            (low - prev_close).abs(), max
        )
        return float(tr.tail(period).mean())
    except Exception as exc:  # pragma: no cover - network/optional dep
        logger.debug("ATR lookup failed for %s: %s", symbol, exc)
        return None


def _sector(symbol: str) -> Optional[str]:
    try:
        from tradingagents.agents.utils.agent_utils import resolve_instrument_identity

        return resolve_instrument_identity(symbol).get("sector")
    except Exception as exc:  # pragma: no cover
        logger.debug("sector lookup failed for %s: %s", symbol, exc)
        return None


def get_quote(symbol: str, cfg: BridgeConfig) -> Optional[MarketQuote]:
    """Live quote for one symbol. Returns ``None`` only if there's no price."""
    try:
        import yfinance as yf

        fast = yf.Ticker(symbol).fast_info
        price = float(fast.get("last_price") or fast.get("lastPrice") or 0.0)
        bid = fast.get("bid")
        ask = fast.get("ask")
    except Exception as exc:  # pragma: no cover
        logger.debug("quote lookup failed for %s: %s", symbol, exc)
        return None

    if not price or price <= 0:
        return None

    atr = _atr(symbol)
    return MarketQuote(
        symbol=symbol,
        price=price,
        stop_frac=cfg.stop_frac(atr, price),
        bid=float(bid) if bid else None,
        ask=float(ask) if ask else None,
        sector=_sector(symbol),
        shortable=True,   # refined live via get_equity_tradability in Phase 2
        halted=False,
    )


def get_quotes(symbols: Iterable[str], cfg: BridgeConfig) -> Dict[str, MarketQuote]:
    quotes: Dict[str, MarketQuote] = {}
    for sym in symbols:
        q = get_quote(sym, cfg)
        if q is not None:
            quotes[sym] = q
        else:
            logger.warning("no quote for %s — it will be skipped", sym)
    return quotes


def quotes_from_fixture(raw: Dict[str, dict], cfg: BridgeConfig) -> Dict[str, MarketQuote]:
    """Build quotes from a fixture dict (offline dry-run).

    Each entry needs at least ``price``; ``atr`` (to derive the stop), ``bid``,
    ``ask``, ``sector``, ``shortable``, ``halted`` are optional.
    """
    quotes: Dict[str, MarketQuote] = {}
    for sym, q in raw.items():
        price = float(q["price"])
        stop = q.get("stop_frac")
        if stop is None:
            stop = cfg.stop_frac(q.get("atr"), price)
        quotes[sym] = MarketQuote(
            symbol=sym,
            price=price,
            stop_frac=float(stop),
            bid=q.get("bid"),
            ask=q.get("ask"),
            sector=q.get("sector"),
            shortable=q.get("shortable", True),
            halted=q.get("halted", False),
        )
    return quotes
