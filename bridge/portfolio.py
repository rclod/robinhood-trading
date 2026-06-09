"""Pre-trade account status + per-position P&L.

Before any trade, the executor computes a portfolio snapshot with unrealized P&L
per holding (current price vs. average cost) plus net liq / buying power, and
surfaces it in the execution payload. This makes "check the account first" an
explicit, structured part of the daily management decision — the agent reviews it
before acting, and it's recorded in the warehouse for review.
"""

from __future__ import annotations

from typing import Dict, Optional

from .models import MarketQuote, PortfolioSnapshot


def portfolio_status(
    snapshot: PortfolioSnapshot,
    quotes: Dict[str, MarketQuote],
    assessments: Optional[Dict[str, str]] = None,
) -> dict:
    assessments = assessments or {}
    rows = []
    total_mv = 0.0
    total_cost = 0.0
    for sym, p in snapshot.positions.items():
        if p.shares == 0:
            continue
        q = quotes.get(sym)
        price = q.price if q else None
        mv = price * p.shares if price is not None else None
        cost_basis = p.avg_cost * p.shares if p.avg_cost else None
        pnl = mv - cost_basis if (mv is not None and cost_basis is not None) else None
        pnl_pct = (pnl / cost_basis * 100.0) if (pnl is not None and cost_basis) else None
        if mv is not None:
            total_mv += mv
        if cost_basis is not None:
            total_cost += cost_basis
        rows.append({
            "symbol": sym,
            "shares": round(p.shares, 6),
            "avg_cost": p.avg_cost,
            "price": round(price, 2) if price is not None else None,
            "market_value": round(mv, 2) if mv is not None else None,
            "unrealized_pnl": round(pnl, 2) if pnl is not None else None,
            "unrealized_pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "rating": assessments.get(sym),
        })
    rows.sort(key=lambda r: (r["unrealized_pnl"] is None, -(r["unrealized_pnl"] or 0)))
    total_pnl = total_mv - total_cost if total_cost else None
    return {
        "net_liq": round(snapshot.equity, 2),
        "buying_power": round(snapshot.buying_power, 2),
        "positions_value": round(total_mv, 2),
        "cash_est": round(snapshot.equity - total_mv, 2),
        "total_unrealized_pnl": round(total_pnl, 2) if total_pnl is not None else None,
        "total_unrealized_pnl_pct": (
            round(total_pnl / total_cost * 100.0, 2) if (total_pnl is not None and total_cost) else None
        ),
        "positions": rows,
    }
