"""Safety guards — the hard envelope that holds even in fully-automated mode.

Guards annotate each :class:`PlannedOrder` with ``approved`` / ``reasons`` and
trim the book to the configured limits. Order of enforcement:

1.  Account-level blocks (apply to every order):
      - ``agentic_allowed`` false  -> the MCP rejects all orders outright.
      - kill switch off            -> plan only, never execute (handled by the
        executor, surfaced here as a plan-wide note).
2.  Per-order eligibility: shorting needs margin + a shortable instrument; a
    halted instrument is untradeable.
3.  Portfolio caps: per-name notional, sector exposure, max open positions,
    max daily new notional. When a cap binds, the lowest-conviction orders are
    rejected first so the strongest signals survive.

PDT is intentionally NOT a guard: the $25k / day-trade-count rule was removed
effective 2026-06-04, and a $25k account never bound it anyway. The ledger
keeps a day-trade counter purely as telemetry.
"""

from __future__ import annotations

from typing import Dict, List

from .config import BridgeConfig
from .models import OrderPlan, PlannedOrder, PortfolioSnapshot

# Most-bullish -> most-bearish; used to rank conviction when a cap binds.
_TIER_RANK = {"Buy": 0, "Overweight": 1, "Hold": 2, "Underweight": 3, "Sell": 4}


def _conviction(order: PlannedOrder) -> int:
    """Lower == stronger conviction (further from Hold)."""
    rank = _TIER_RANK.get(order.rating.capitalize(), 2)
    return -abs(rank - 2)  # Buy/Sell -> -2 (strongest); Hold -> 0 (weakest)


def _reduces_risk(order: PlannedOrder) -> bool:
    """True if the order shrinks the position (trim or exit), not grows it.

    Exposure caps (per-name, sector, daily notional) exist to bound risk
    *taking*, so they must never block an order that *reduces* exposure — that
    would trap the account in an oversized position. A long→short flip grows
    short exposure, so it is NOT a reduction.
    """
    if order.crosses_zero:
        return False
    return abs(order.target_shares) < abs(order.current_shares)


def _target_notional(order: PlannedOrder) -> float:
    """Notional of the resulting target position (not the order delta)."""
    price = order.limit_price or (order.notional / order.quantity if order.quantity else 0.0)
    return abs(order.target_shares) * price


def apply_guards(
    plan: OrderPlan,
    snapshot: PortfolioSnapshot,
    cfg: BridgeConfig,
) -> OrderPlan:
    """Run all guards in place and return the (now annotated) plan."""
    equity = snapshot.equity

    # 1. Account-level blocks.
    if not snapshot.agentic_allowed:
        for o in plan.orders:
            o.reject("account not agentic_allowed — MCP would reject")
        plan.notes.append("BLOCKED: account is not agentic_allowed")

    if not cfg.execution_enabled:
        plan.execution_enabled = False
        plan.notes.append("kill switch off (BRIDGE_ENABLED unset) — dry-run, nothing placed")

    # 2. Per-order eligibility.
    for o in plan.orders:
        if o.halted:
            o.reject("instrument halted")
        is_short_exposure = o.target_shares < 0
        if is_short_exposure and not snapshot.margin_enabled:
            o.reject("short target but account has no margin")
        if is_short_exposure and not o.shortable:
            o.reject("instrument not shortable")
        # per-name ceiling on the resulting TARGET position (not the order
        # delta), and only for risk-increasing orders — a trim/exit can never
        # breach a position-size cap. Sizing already caps targets, so this is
        # defense-in-depth.
        if not _reduces_risk(o) and _target_notional(o) > cfg.per_name_cap * equity + 1e-6:
            o.reject(
                f"target position ${_target_notional(o):,.0f} exceeds per-name cap "
                f"${cfg.per_name_cap * equity:,.0f}"
            )

    # 3. Portfolio caps — only risk-INCREASING orders consume cap budget.
    # Risk-reducing orders (trims/exits) bypass these entirely so the bridge can
    # always shed exposure. Increasing orders are checked strongest-first.
    increasing = [o for o in plan.approved_orders if not _reduces_risk(o)]

    # 3a. sector cap (target exposure of increasing orders)
    sector_notional: Dict[str, float] = {}
    for o in sorted(increasing, key=_conviction):
        if not o.approved:
            continue
        sec = o.sector or "UNKNOWN"
        used = sector_notional.get(sec, 0.0)
        if used + _target_notional(o) > cfg.sector_cap * equity + 1e-6:
            o.reject(
                f"sector '{sec}' exposure would exceed cap "
                f"${cfg.sector_cap * equity:,.0f}"
            )
        else:
            sector_notional[sec] = used + _target_notional(o)

    # 3b. max open positions — count names that would be open after the book.
    open_after = set(s for s, p in snapshot.positions.items() if p.shares != 0)
    kept = 0
    for o in sorted(plan.approved_orders, key=_conviction):
        # an order that flattens (target 0) frees a slot; one that opens uses one
        opens = o.target_shares != 0 and o.symbol not in open_after
        if opens:
            if len(open_after) + kept >= cfg.max_positions:
                o.reject(f"max_positions ({cfg.max_positions}) reached")
                continue
            kept += 1

    # 3c. max daily new notional — optional turnover throttle (off by default;
    # the dry-powder budget + buying-power guard already bound deployment).
    if cfg.max_daily_notional is not None:
        spent = 0.0
        for o in sorted(increasing, key=_conviction):
            if not o.approved:
                continue
            if spent + o.notional > cfg.max_daily_notional + 1e-6:
                o.reject(f"daily notional cap ${cfg.max_daily_notional:,.0f} reached")
            else:
                spent += o.notional

    # 3d. buying-power ceiling (hard on a cash account — sells free cash, buys
    # consume it; in a cash account you can't spend unsettled/absent funds).
    if not snapshot.margin_enabled:
        buys = 0.0
        for o in sorted(plan.approved_orders, key=_conviction):
            if o.side != "buy":
                continue
            if buys + o.notional > snapshot.buying_power + 1e-6:
                o.reject(
                    f"insufficient buying power "
                    f"(${snapshot.buying_power:,.0f}) for cash account"
                )
            else:
                buys += o.notional

    return plan
