"""Position sizing: the one formula that governs money risk.

Reconciles the user's %-of-equity sizing with the 1%-risk golden rule by taking
the **smaller** of the two, then a hard per-name ceiling:

    notional = min( |tier%| x equity ,  risk_per_trade x equity / stop_frac )
    notional = min( notional , per_name_cap x equity )
    target_shares = sign(tier) x floor(notional / price)   # whole shares only

Conviction (the tier %) scales size *up to* a fixed risk ceiling — a wide stop
can never let a single trade lose more than ``risk_per_trade`` of equity.
Whole-share rounding is mandatory because shorting blocks fractional orders.
"""

from __future__ import annotations

import math

from .config import BridgeConfig
from .policy import target_weight


def stop_frac_for(cfg: BridgeConfig, atr, price) -> float:
    """Public wrapper so callers don't reach into the config for the stop."""
    return cfg.stop_frac(atr, price)


def target_shares(
    rating: str,
    equity: float,
    price: float,
    stop_frac: float,
    current_shares: float,
    cfg: BridgeConfig,
    allow_short: bool = True,
) -> float:
    """Compute the signed target share count for one name.

    Hold (and any zero-weight rating) returns ``current_shares`` so reconcile
    produces a zero delta — the position is carried untouched. When
    ``allow_short`` is False (cash account, or shorting disabled), a bearish
    rating targets flat (0) instead of a negative position — i.e. exit any long
    rather than open a short.
    """
    weight = target_weight(rating, cfg)
    if weight is None:
        return current_shares  # carry

    if weight < 0 and not allow_short:
        return 0.0  # exit to flat; never go short on a cash account

    if price <= 0:
        return current_shares  # cannot size without a price; leave as-is

    tier_notional = abs(weight) * equity
    # Guard against a zero/negative stop slipping through.
    safe_stop = max(stop_frac, 1e-6)
    risk_notional = (cfg.risk_per_trade * equity) / safe_stop
    cap_notional = cfg.per_name_cap * equity

    notional = min(tier_notional, risk_notional, cap_notional)
    shares = math.floor(notional / price)
    return math.copysign(shares, weight) if shares else 0.0


def target_notional(rating, equity, stop_frac, cfg, allow_short=True):
    """Signed target dollar exposure for a name — the capital-agnostic, price-
    independent recommendation. ``None`` means carry (Hold). Bearish on a
    cash account (``allow_short=False``) targets flat (0)."""
    weight = target_weight(rating, cfg)
    if weight is None:
        return None  # carry
    if weight < 0 and not allow_short:
        return 0.0  # exit to flat
    safe_stop = max(stop_frac, 1e-6)
    notional = min(
        abs(weight) * equity,
        (cfg.risk_per_trade * equity) / safe_stop,
        cfg.per_name_cap * equity,
    )
    return math.copysign(notional, weight)
