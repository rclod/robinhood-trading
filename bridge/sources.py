"""Pluggable inputs: ratings and the portfolio snapshot.

Both inputs are abstracted so the *same* decision pipeline runs in dry-run
(cheap, offline, from a fixture) and live (expensive ratings via propagate;
snapshot from the Robinhood MCP, populated by the executor agent).

Ratings:
  - :func:`ratings_from_fixture` — a plain ``{symbol: rating}`` dict.
  - :func:`ratings_from_propagate` — runs TradingAgents on each watchlist name
    (one multi-minute, multi-LLM run per ticker; needs provider API keys).

Portfolio:
  - :func:`snapshot_from_fixture` — built from a JSON file.
  - The live snapshot is assembled by the executor from MCP results and passed
    straight into :class:`~bridge.models.PortfolioSnapshot`; there is no
    Python-side MCP call because those tools are agent-side, not a library.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Dict, Optional

from .config import BridgeConfig
from .models import Position, PortfolioSnapshot

logger = logging.getLogger(__name__)


# --- ratings ---------------------------------------------------------------


def ratings_from_fixture(raw: Dict[str, str]) -> Dict[str, str]:
    """Normalise a fixture ``{symbol: rating}`` map to canonical casing."""
    return {sym.upper(): rating.capitalize() for sym, rating in raw.items()}


def _prepare_propagate_config(config: Optional[dict]) -> dict:
    """Build the TradingAgents config for a propagate run, routing the scarce
    Alpha Vantage quota to its highest-value use.

    AV's free tier is tiny (~25 req/day), so we spend it only on NEWS (timestamped
    + sentiment-scored, incl. the macro "big news") — fundamentals/prices/indicators
    stay on free yfinance. The vendor string ``"alpha_vantage,yfinance"`` makes
    route_to_vendor try AV first and fall back to yfinance automatically once the
    quota is hit, so the priority names (run first) get AV and the rest degrade
    cleanly. Disable with BRIDGE_AV_NEWS=0.
    """
    from tradingagents.default_config import DEFAULT_CONFIG

    cfg = (config or DEFAULT_CONFIG).copy()
    if os.getenv("ALPHA_VANTAGE_API_KEY") and _env_truthy("BRIDGE_AV_NEWS", default=True):
        vendors = dict(cfg.get("data_vendors", {}))  # copy nested dict (shallow .copy())
        vendors["news_data"] = "alpha_vantage,yfinance"
        cfg["data_vendors"] = vendors
        logger.info("Alpha Vantage routed for news (yfinance fallback on quota)")
    _install_macro_news_cache()
    return cfg


def _install_macro_news_cache(iface=None) -> None:
    """Memoize ``get_global_news`` for the lifetime of a compute run.

    Macro/global news is identical for every ticker on a given date, so without
    this each of the ~29 ticker analyses re-fetches the same article set — wasting
    the tiny Alpha Vantage quota (and time). We wrap the vendor implementations in
    ``VENDOR_METHODS["get_global_news"]`` with an in-process cache keyed on the call
    args (date/look-back/limit), so the macro news is fetched ONCE and reused.
    Idempotent; ticker-specific ``get_news`` is left uncached (it genuinely differs).
    """
    import functools

    if iface is None:
        try:
            import tradingagents.dataflows.interface as iface  # type: ignore
        except Exception as exc:  # pragma: no cover
            logger.debug("macro-news cache not installed: %s", exc)
            return

    methods = getattr(iface, "VENDOR_METHODS", {}).get("get_global_news")
    if not methods:
        return
    for vendor, impl in list(methods.items()):
        func = impl[0] if isinstance(impl, list) else impl
        if getattr(func, "_bridge_cached", False):
            continue
        cached = functools.lru_cache(maxsize=16)(func)
        cached._bridge_cached = True
        methods[vendor] = [cached] if isinstance(impl, list) else cached


def _env_truthy(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def ratings_from_propagate(
    watchlist, trade_date: str, config: Optional[dict] = None
) -> Dict[str, str]:
    """Run TradingAgents on each watchlist name and collect the 5-tier rating.

    Lazy-imports the heavy graph so dry-run never pays for it. Each ticker is a
    full, slow, billable run — this is the live ratings source.
    """
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    cfg = _prepare_propagate_config(config)
    graph = TradingAgentsGraph(debug=False, config=cfg)

    ratings: Dict[str, str] = {}
    for symbol in watchlist:
        try:
            _, rating = graph.propagate(symbol, trade_date)
            ratings[symbol.upper()] = rating
            logger.info("%s -> %s", symbol, rating)
        except Exception as exc:  # one bad name shouldn't sink the whole book
            logger.error("propagate failed for %s: %s", symbol, exc)
    return ratings


_PRICE_TARGET_RE = re.compile(r"price\s*target\**\s*:?\s*\$?\s*([\d,]+\.?\d*)", re.IGNORECASE)


def _parse_price_target(decision_text: str) -> Optional[float]:
    """Pull the PM's '**Price Target**: X' out of the rendered decision markdown."""
    m = _PRICE_TARGET_RE.search(decision_text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def signals_from_propagate(
    watchlist, trade_date: str, config: Optional[dict] = None
) -> Dict[str, dict]:
    """Like :func:`ratings_from_propagate` but also captures the PM's price target.

    Returns ``{SYMBOL: {"rating": str, "price_target": float|None}}``. The price
    target feeds the conviction score used to prioritise funding within a tier.
    """
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    cfg = _prepare_propagate_config(config)
    graph = TradingAgentsGraph(debug=False, config=cfg)

    signals: Dict[str, dict] = {}
    for symbol in watchlist:
        try:
            final_state, rating = graph.propagate(symbol, trade_date)
            pt = _parse_price_target(final_state.get("final_trade_decision", ""))
            signals[symbol.upper()] = {"rating": rating, "price_target": pt}
            logger.info("%s -> %s (target=%s)", symbol, rating, pt)
        except Exception as exc:
            logger.error("propagate failed for %s: %s", symbol, exc)
    return signals


# --- portfolio -------------------------------------------------------------


def _unwrap_data(d: dict) -> dict:
    """Tolerate a raw MCP response (``{"data": {...}}``) vs. the inner data dict."""
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        return d["data"]
    return d


def _select_account(raw: dict, account_number: Optional[str]) -> dict:
    """Accept any shape an agent might write for the account: the single account
    object, ``{"accounts": [...]}``, or the raw ``{"data": {"accounts": [...]}}``.
    Selects by ``account_number`` when given, else the agentic account, else first.
    """
    raw = _unwrap_data(raw)
    accounts = raw.get("accounts") if isinstance(raw, dict) else None
    if accounts is None:
        return raw  # already a single account object
    if account_number:
        for a in accounts:
            if str(a.get("account_number")) == str(account_number):
                return a
    for a in accounts:
        if a.get("agentic_allowed"):
            return a
    return accounts[0] if accounts else {}


def snapshot_from_mcp(
    account: dict,
    portfolio_data: dict,
    positions_data: dict,
    cfg: BridgeConfig,
    account_number: Optional[str] = None,
) -> PortfolioSnapshot:
    """Build a snapshot from raw Robinhood MCP ``data`` payloads.

    This is the Phase-2 executor's adapter: the agent fetches ``get_accounts``,
    ``get_portfolio`` and ``get_equity_positions`` and passes their ``data``
    dicts here. Margin capability is inferred from the account ``type`` —
    ``cash`` accounts can't short, so the bridge runs longs-only against them.

    - equity base = portfolio ``total_value`` (whole-account value)
    - ``buying_power`` = the broker's authoritative spendable figure
    - sellable share counts use ``shares_available_for_sells`` when present
    """
    account = _select_account(account, account_number or cfg.account_number)
    portfolio_data = _unwrap_data(portfolio_data)
    positions_data = _unwrap_data(positions_data)

    total_value = float(portfolio_data.get("total_value") or 0.0)
    bp = portfolio_data.get("buying_power", {})
    buying_power = float(
        (bp.get("buying_power") if isinstance(bp, dict) else bp) or 0.0
    )

    positions = {}
    for p in positions_data.get("positions", []):
        sym = p["symbol"].upper()
        shares = float(p.get("shares_available_for_sells", p.get("quantity", 0)) or 0)
        if p.get("type") == "short":
            shares = -abs(shares)
        positions[sym] = Position(
            symbol=sym,
            shares=shares,
            avg_cost=float(p["average_buy_price"]) if p.get("average_buy_price") else None,
        )

    return PortfolioSnapshot(
        account_number=account.get("account_number") or cfg.account_number,
        equity=total_value,
        buying_power=buying_power,
        positions=positions,
        agentic_allowed=bool(account.get("agentic_allowed", False)),
        margin_enabled=account.get("type") == "margin",
    )


def snapshot_from_fixture(raw: dict, cfg: BridgeConfig) -> PortfolioSnapshot:
    """Build a :class:`PortfolioSnapshot` from a fixture/JSON dict."""
    positions = {
        sym.upper(): Position(
            symbol=sym.upper(),
            shares=float(p["shares"]),
            avg_cost=p.get("avg_cost"),
        )
        for sym, p in raw.get("positions", {}).items()
    }
    return PortfolioSnapshot(
        account_number=raw.get("account_number") or cfg.account_number,
        equity=float(raw["equity"]),
        buying_power=float(raw.get("buying_power", raw["equity"])),
        positions=positions,
        agentic_allowed=bool(raw.get("agentic_allowed", False)),
        margin_enabled=bool(raw.get("margin_enabled", False)),
    )
