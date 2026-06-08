"""Rotation / capital-allocation: deploy buying power to the best names.

The base pipeline (``build_order_plan``) sizes each name independently. Rotation
adds the portfolio view the user needs before going live: rank ALL rated names,
fund the strongest buy candidates from **settled buying power**, scaling the
marginal buy to fully deploy capital, and produce an auditable report of what
got funded vs deferred and why.

Key rules:
  - **Reductions (sells: trims/exits) always execute** — they're risk-reducing
    and free of budget. They are processed first.
  - **Buys are budget-constrained.** Budget = settled ``buying_power``. On a cash
    account, same-day sale proceeds are unsettled (T+1) and are deliberately NOT
    counted, so the plan never spends money it doesn't have today.
  - Buy candidates are ranked strongest-conviction first (Buy before Overweight,
    larger desired size as tiebreak), then funded greedily subject to per-name,
    sector, and max-position caps. The marginal candidate is scaled to fit.

The result is a normal ``OrderPlan`` (so the executor consumes it unchanged) with
a ``rotation`` report attached.
"""

from __future__ import annotations

import math
from typing import Dict, List

from .config import BridgeConfig
from .guards import _TIER_RANK, apply_guards
from .models import MarketQuote, OrderPlan, PortfolioSnapshot
from .reconcile import _order_for
from .sizing import target_shares


def _conviction(rating: str) -> int:
    return _TIER_RANK.get(rating.capitalize(), 2)  # 0=Buy strongest


def _cand(symbol, rating, desired, funded, status, reason=""):
    return {
        "symbol": symbol, "rating": rating, "rank": None,
        "desired_notional": round(desired, 2), "funded_notional": round(funded, 2),
        "status": status, "reason": reason,
    }


def build_rotation_plan(
    trade_date: str,
    ratings: Dict[str, str],
    snapshot: PortfolioSnapshot,
    quotes: Dict[str, MarketQuote],
    cfg: BridgeConfig,
) -> OrderPlan:
    """Build a guarded, budget-allocated rotation plan from ratings + state."""
    allow_short = cfg.allow_short and snapshot.margin_enabled
    equity = snapshot.equity
    plan = OrderPlan(trade_date=trade_date, equity=equity,
                     execution_enabled=cfg.execution_enabled)
    if cfg.allow_short and not snapshot.margin_enabled:
        plan.notes.append("cash account (no margin) — longs-only; bearish ratings exit to flat")

    # 1. Desired (full-conviction) target per rated name.
    desired = {}
    for sym, rating in ratings.items():
        plan.assessments[sym] = rating
        q = quotes.get(sym)
        if q is None:
            plan.notes.append(f"{sym}: no quote — skipped")
            continue
        cur = snapshot.shares_of(sym)
        stop = q.stop_frac if q.stop_frac else cfg.stop_fallback
        tgt = target_shares(rating, equity, q.price, stop, cur, cfg, allow_short=allow_short)
        desired[sym] = (rating, cur, tgt, q)

    # 2. Split into reductions (sells) and increases (buys).
    reductions, increases = [], []
    for sym, (rating, cur, tgt, q) in desired.items():
        delta = tgt - cur
        if delta <= -1:
            reductions.append((sym, rating, cur, tgt, q))
        elif delta >= 1:
            increases.append((sym, rating, cur, tgt, q))
        else:
            plan.holds.append(sym)

    # 3. Reductions always execute (risk-reducing, no budget needed).
    for sym, rating, cur, tgt, q in reductions:
        o = _order_for(sym, cur, tgt, q, trade_date, cfg, rating)
        (plan.orders.append(o) if o else plan.holds.append(sym))

    # 4. Rank buy candidates strongest-first; tiebreak larger desired add.
    def _price(q):
        return q.ask or q.price

    increases.sort(key=lambda it: (_conviction(it[1]), -((it[3] - it[2]) * _price(it[4]))))

    # 5. Greedily fund from settled buying power, respecting caps; scale marginal.
    budget = max(0.0, snapshot.buying_power)
    deployed = 0.0
    open_names = {s for s, p in snapshot.positions.items() if p.shares != 0}
    sector_used: Dict[str, float] = {}
    for s, p in snapshot.positions.items():
        q = quotes.get(s)
        if q and p.shares > 0:
            sector_used[q.sector or "UNKNOWN"] = sector_used.get(q.sector or "UNKNOWN", 0.0) + p.shares * q.price

    candidates: List[dict] = []
    for rank, (sym, rating, cur, tgt, q) in enumerate(increases):
        price = _price(q)
        desired_notional = (tgt - cur) * price
        sec = q.sector or "UNKNOWN"

        if cur == 0 and len(open_names) >= cfg.max_positions:
            candidates.append(_cand(sym, rating, desired_notional, 0, "deferred", "max_positions reached"))
            continue

        per_name_room = cfg.per_name_cap * equity - cur * price
        sector_room = cfg.sector_cap * equity - sector_used.get(sec, 0.0)
        budget_room = budget - deployed
        room = min(desired_notional, max(0.0, per_name_room), max(0.0, sector_room), max(0.0, budget_room))

        if room < price:  # can't afford a single share
            reason = "insufficient settled buying power" if budget_room < price else "per-name/sector cap reached"
            candidates.append(_cand(sym, rating, desired_notional, 0, "deferred", reason))
            continue

        add_shares = min(tgt - cur, math.floor(room / price))
        if add_shares < 1:
            candidates.append(_cand(sym, rating, desired_notional, 0, "deferred", "no whole share fits"))
            continue

        new_tgt = cur + add_shares
        o = _order_for(sym, cur, new_tgt, q, trade_date, cfg, rating)
        if o is None:
            plan.holds.append(sym)
            continue
        plan.orders.append(o)
        funded = add_shares * price
        deployed += funded
        sector_used[sec] = sector_used.get(sec, 0.0) + new_tgt * price
        if cur == 0:
            open_names.add(sym)
        status = "funded" if add_shares == (tgt - cur) else "scaled"
        reason = "" if status == "funded" else f"scaled to fit ${room:,.0f} of room"
        row = _cand(sym, rating, desired_notional, funded, status, reason)
        row["rank"] = rank
        candidates.append(row)

    # 6. Guards (defense-in-depth; allocation already respects caps + budget).
    plan = apply_guards(plan, snapshot, cfg)

    # 7. Attach the rotation report.
    funded_n = sum(1 for c in candidates if c["status"] in ("funded", "scaled"))
    deferred_n = sum(1 for c in candidates if c["status"] == "deferred")
    plan.rotation = {
        "buying_power": round(budget, 2),
        "deployed": round(deployed, 2),
        "remaining": round(budget - deployed, 2),
        "candidates": candidates,
        "sells": [{"symbol": s, "rating": r} for s, r, _, _, _ in reductions],
    }
    plan.notes.append(
        f"rotation: ${deployed:,.0f}/${budget:,.0f} settled BP deployed across "
        f"{funded_n} buy(s); {deferred_n} deferred"
    )
    return plan
