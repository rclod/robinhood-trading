"""Funding layer — move toward the recommended book with today's settled cash.

Takes the capital-agnostic :mod:`recommend` target book and produces orders:

  - **Reductions (trim/exit) execute fully** — risk-reducing, no budget needed.
  - **Buys are funded conviction-first** (Buy before Overweight; within a tier,
    names already held first, then alphabetical) using **fractional dollar
    amounts**, so share price never strands capital or biases which names get
    bought. The marginal name is scaled to the remaining budget.
  - **Dry powder:** a reserve (``cash_reserve_frac`` of equity) is held back
    every day. On a cash account sells settle T+1, so deploying 100% today would
    leave nothing settled to act on tomorrow.

Selection of *what* to buy comes entirely from the recommendation (conviction).
This module only decides *how much* of each to fund given the cash on hand.
"""

from __future__ import annotations

import math
from typing import Dict, List

from .config import BridgeConfig, is_etf
from .guards import apply_guards
from .models import MarketQuote, OrderPlan, PlannedOrder, PortfolioSnapshot
from .reconcile import _order_for, _ref_id
from .recommend import build_recommendation

MIN_BUY = 1.0  # Robinhood fractional minimum (USD)


def _dollar_buy(symbol, amount, quote, current_shares, trade_date, rating, cfg):
    """A fractional/dollar-based market buy order."""
    price = quote.ask or quote.price
    approx_shares = amount / price if price else 0.0
    return PlannedOrder(
        symbol=symbol, side="buy", quantity=round(approx_shares, 6),
        order_type="market", limit_price=None, notional=round(amount, 2),
        dollar_amount=round(amount, 2), rating=rating, sector=quote.sector,
        target_shares=current_shares + approx_shares, current_shares=current_shares,
        crosses_zero=False, ref_id=_ref_id(symbol, trade_date, "buy"),
        shortable=quote.shortable, halted=quote.halted,
    )


def build_rotation_plan(
    trade_date: str,
    ratings: Dict[str, str],
    snapshot: PortfolioSnapshot,
    quotes: Dict[str, MarketQuote],
    cfg: BridgeConfig,
    price_targets: Dict[str, float] | None = None,
) -> OrderPlan:
    rec = build_recommendation(trade_date, ratings, snapshot, quotes, cfg, price_targets)
    equity = snapshot.equity
    plan = OrderPlan(trade_date=trade_date, equity=equity,
                     execution_enabled=cfg.execution_enabled)
    plan.assessments = {t.symbol: t.rating for t in rec.targets}
    if cfg.allow_short and not snapshot.margin_enabled:
        plan.notes.append("cash account (no margin) — longs-only; bearish ratings exit to flat")

    held = {s for s, p in snapshot.positions.items() if p.shares != 0}

    # 1. Reductions (trim/exit) — execute fully, regardless of budget.
    for t in rec.targets:
        if t.action not in ("trim", "exit"):
            continue
        q = quotes[t.symbol]
        cur = snapshot.shares_of(t.symbol)
        tgt_shares = 0.0 if t.action == "exit" else max(0.0, math.floor(t.target_notional / q.price))
        o = _order_for(t.symbol, cur, tgt_shares, q, trade_date, cfg, t.rating)
        if o:
            plan.orders.append(o)
        else:
            plan.holds.append(t.symbol)
    for t in rec.targets:
        if t.action == "hold":
            plan.holds.append(t.symbol)

    # 2. Buys — conviction-priority, fractional, within the dry-powder budget.
    # Reserve is a fraction of settled buying power (spendable), so we always
    # keep some of today's cash liquid for tomorrow (sells settle T+1).
    reserve = cfg.cash_reserve_frac * snapshot.buying_power
    budget = max(0.0, snapshot.buying_power - reserve)
    deployed = 0.0
    sector_used: Dict[str, float] = {}
    etf_cap = cfg.etf_sleeve_frac * equity   # bounded ETF sleeve
    etf_used = 0.0
    for s, p in snapshot.positions.items():
        q = quotes.get(s)
        if q and p.shares > 0:
            sec = q.sector or "UNKNOWN"
            sector_used[sec] = sector_used.get(sec, 0.0) + p.shares * q.price
            if is_etf(s):
                etf_used += p.shares * q.price

    # Fund strongest first: tier, then finer conviction score (price-target
    # upside), then held-names, then name. Score replaces the old arbitrary
    # alphabetical tiebreak among equal-tier names.
    adds = [t for t in rec.targets if t.action == "add"]
    adds.sort(key=lambda t: (t.conviction, -t.score, 0 if t.symbol in held else 1, t.symbol))

    candidates: List[dict] = []
    for t in adds:
        q = quotes[t.symbol]
        sec = q.sector or "UNKNOWN"
        etf = is_etf(t.symbol)
        per_name_room = cfg.per_name_cap * equity - t.current_notional
        sector_room = cfg.sector_cap * equity - sector_used.get(sec, 0.0)
        rooms = [t.delta_notional, max(0.0, per_name_room),
                 max(0.0, sector_room), max(0.0, budget - deployed)]
        if etf:  # bounded ETF sleeve
            rooms.append(max(0.0, etf_cap - etf_used))
        room = min(rooms)
        if room < MIN_BUY:
            if etf and (etf_cap - etf_used) < MIN_BUY:
                reason = "ETF sleeve cap reached"
            elif (budget - deployed) < MIN_BUY:
                reason = "dry-powder budget reached"
            else:
                reason = "per-name/sector cap reached"
            candidates.append(_cand(t, 0.0, "deferred", reason))
            continue
        amount = round(room, 2)
        plan.orders.append(_dollar_buy(t.symbol, amount, q, snapshot.shares_of(t.symbol),
                                       trade_date, t.rating, cfg))
        deployed += amount
        sector_used[sec] = sector_used.get(sec, 0.0) + (t.current_notional + amount)
        if etf:
            etf_used += amount
        status = "funded" if amount >= t.delta_notional - 0.01 else "scaled"
        candidates.append(_cand(t, amount, status,
                                "" if status == "funded" else f"scaled to ${room:,.0f} remaining"))

    plan = apply_guards(plan, snapshot, cfg)

    plan.rotation = {
        "buying_power": round(snapshot.buying_power, 2),
        "reserve": round(reserve, 2),
        "budget": round(budget, 2),
        "deployed": round(deployed, 2),
        "dry_powder": round(snapshot.buying_power - deployed, 2),
        "etf_sleeve_cap": round(etf_cap, 2),
        "etf_deployed": round(etf_used, 2),
        "candidates": candidates,
        "recommendation": [
            {"symbol": t.symbol, "rating": t.rating, "score": t.score, "action": t.action,
             "target_notional": t.target_notional, "current_notional": t.current_notional,
             "delta_notional": t.delta_notional}
            for t in rec.by_conviction()
        ],
    }
    funded_n = sum(1 for c in candidates if c["status"] in ("funded", "scaled"))
    plan.notes.append(
        f"funding: ${deployed:,.0f} deployed / ${budget:,.0f} budget "
        f"(${reserve:,.0f} dry-powder reserve held); {funded_n} buy(s)"
    )
    return plan


def _cand(t, funded, status, reason=""):
    return {
        "symbol": t.symbol, "rating": t.rating, "score": t.score,
        "desired_notional": t.delta_notional, "funded_notional": round(funded, 2),
        "status": status, "reason": reason,
    }
