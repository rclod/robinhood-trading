"""Recommendation layer — the capital-agnostic target book.

Converts ratings into a target portfolio in *dollar/weight* terms for EVERY
rated name, independent of how much buying power exists. This is "what the book
should look like, fully funded" — the funding layer (allocate.py) then decides
how to move toward it with today's settled capital.

Keeping this separate is deliberate: a name's recommendation must come from
conviction, never from whether its share price happens to be affordable. A $900
Overweight name and a $90 Overweight name get the same target weight here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .config import BridgeConfig
from .guards import _TIER_RANK
from .models import MarketQuote, PortfolioSnapshot
from .sizing import target_notional


@dataclass
class Target:
    symbol: str
    rating: str
    conviction: int            # 0 = strongest (Buy); 5-tier rank
    target_notional: float     # signed desired $ exposure (capital-agnostic)
    current_notional: float
    delta_notional: float      # target - current ( >0 add, <0 reduce )
    action: str                # add | trim | exit | hold


@dataclass
class Recommendation:
    equity: float
    targets: List[Target]

    def by_conviction(self) -> List[Target]:
        return sorted(self.targets, key=lambda t: (t.conviction, t.symbol))


def build_recommendation(
    trade_date: str,
    ratings: Dict[str, str],
    snapshot: PortfolioSnapshot,
    quotes: Dict[str, MarketQuote],
    cfg: BridgeConfig,
) -> Recommendation:
    allow_short = cfg.allow_short and snapshot.margin_enabled
    targets: List[Target] = []
    for sym, rating in ratings.items():
        q = quotes.get(sym)
        if q is None:
            continue
        cur_notional = snapshot.shares_of(sym) * q.price
        stop = q.stop_frac if q.stop_frac else cfg.stop_fallback
        tn = target_notional(rating, snapshot.equity, stop, cfg, allow_short)
        if tn is None:                       # Hold / carry
            tn = cur_notional
            action = "hold"
        elif tn > cur_notional + 1:
            action = "add"
        elif tn < cur_notional - 1:
            action = "exit" if tn <= 0 else "trim"
        else:
            action = "hold"
        targets.append(Target(
            symbol=sym, rating=rating,
            conviction=_TIER_RANK.get(rating.capitalize(), 2),
            target_notional=round(tn, 2), current_notional=round(cur_notional, 2),
            delta_notional=round(tn - cur_notional, 2), action=action,
        ))
    return Recommendation(equity=snapshot.equity, targets=targets)
