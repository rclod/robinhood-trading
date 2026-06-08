"""Turn target positions into delta orders.

A rating is a *target state*; an order is a *delta*. For each rated name we
compute ``delta = target_shares - current_shares`` and emit a single order for
the difference. Zero-delta names (Hold/carry, or already at target) become
"holds" rather than orders. A delta whose sign flips the position through zero
(long -> short or vice-versa) is flagged ``crosses_zero`` so the executor and
the guards can treat it with extra care.
"""

from __future__ import annotations

import uuid
from typing import Dict, Optional

from .config import BridgeConfig
from .models import MarketQuote, OrderPlan, PlannedOrder, PortfolioSnapshot
from .sizing import stop_frac_for, target_shares

# Stable namespace so ref_ids are deterministic across re-runs of the same day.
_REF_NS = uuid.uuid5(uuid.NAMESPACE_URL, "tradingagents.bridge.refid")


def _ref_id(symbol: str, trade_date: str, side: str) -> str:
    return str(uuid.uuid5(_REF_NS, f"{symbol}:{trade_date}:{side}"))


def _limit_price(quote: MarketQuote, side: str, cfg: BridgeConfig) -> Optional[float]:
    """Marketable limit: cross the spread slightly toward a fill."""
    if cfg.order_type != "limit":
        return None
    off = cfg.marketable_offset
    if side == "buy":
        base = quote.ask or quote.price
        return round(base * (1 + off), 2)
    base = quote.bid or quote.price
    return round(base * (1 - off), 2)


def build_plan(
    trade_date: str,
    ratings: Dict[str, str],
    snapshot: PortfolioSnapshot,
    quotes: Dict[str, MarketQuote],
    cfg: BridgeConfig,
) -> OrderPlan:
    """Build the raw (pre-guard) order plan from ratings + current state."""
    plan = OrderPlan(
        trade_date=trade_date,
        equity=snapshot.equity,
        execution_enabled=cfg.execution_enabled,
    )

    # Shorting requires both intent (config) and capability (margin account).
    allow_short = cfg.allow_short and snapshot.margin_enabled
    if cfg.allow_short and not snapshot.margin_enabled:
        plan.notes.append("cash account (no margin) — longs-only; bearish ratings exit to flat")

    for symbol, rating in ratings.items():
        plan.assessments[symbol] = rating  # record every rating, traded or not
        quote = quotes.get(symbol)
        if quote is None:
            plan.notes.append(f"{symbol}: no quote — skipped")
            continue

        current = snapshot.shares_of(symbol)
        stop = stop_frac_for(cfg, None, quote.price) if quote.stop_frac is None else quote.stop_frac
        tgt = target_shares(
            rating, snapshot.equity, quote.price, stop, current, cfg,
            allow_short=allow_short,
        )
        order = _order_for(symbol, current, tgt, quote, trade_date, cfg, rating)
        if order is None:
            plan.holds.append(symbol)
        else:
            plan.orders.append(order)

    return plan


def _order_for(symbol, current, tgt, quote, trade_date, cfg, rating):
    """Build a single delta order, or None when the move is sub-one-share."""
    delta = tgt - current
    if abs(delta) < 1:  # whole-share threshold; nothing to do
        return None

    side = "buy" if delta > 0 else "sell"
    qty = abs(delta)
    crosses = current != 0 and tgt != 0 and (current > 0) != (tgt > 0)
    limit = _limit_price(quote, side, cfg)
    notional = qty * (limit or quote.price)

    return PlannedOrder(
        symbol=symbol,
        side=side,
        quantity=qty,
        order_type=cfg.order_type,
        limit_price=limit,
        notional=notional,
        rating=rating,
        sector=quote.sector,
        target_shares=tgt,
        current_shares=current,
        crosses_zero=crosses,
        ref_id=_ref_id(symbol, trade_date, side),
        shortable=quote.shortable,
        halted=quote.halted,
    )


def build_plan_from_targets(
    trade_date: str,
    targets: Dict[str, float],
    snapshot: PortfolioSnapshot,
    quotes: Dict[str, MarketQuote],
    cfg: BridgeConfig,
    labels: Dict[str, str] | None = None,
) -> OrderPlan:
    """Build a pre-guard plan from EXPLICIT per-symbol target share counts.

    Used by the intraday risk monitor, which decides targets directly (e.g.
    "exit NVDA to 0") rather than via the rating→sizing path. ``labels`` gives a
    human reason per symbol (shown as the order's ``rating`` field, e.g.
    "intraday-stop"); defaults to "intraday".
    """
    labels = labels or {}
    plan = OrderPlan(
        trade_date=trade_date,
        equity=snapshot.equity,
        execution_enabled=cfg.execution_enabled,
    )
    for symbol, tgt in targets.items():
        quote = quotes.get(symbol)
        if quote is None:
            plan.notes.append(f"{symbol}: no quote — skipped")
            continue
        current = snapshot.shares_of(symbol)
        order = _order_for(
            symbol, current, tgt, quote, trade_date, cfg, labels.get(symbol, "intraday")
        )
        if order is None:
            plan.holds.append(symbol)
        else:
            plan.orders.append(order)
    return plan
