"""Rating -> target stance.

Maps the Portfolio Manager's 5-tier rating to a signed target weight. The one
non-obvious rule is ``Hold``: it means *carry the existing position*, never
"go flat". Returning ``None`` for Hold signals "target == current" to the
sizing layer, which keeps the pipeline a clean target-vs-current reconciliation
for every other tier.
"""

from __future__ import annotations

from typing import Optional

from .config import BridgeConfig


def target_weight(rating: str, cfg: BridgeConfig) -> Optional[float]:
    """Return the signed target weight for a rating.

    ``None`` means "no explicit target — carry current position" (Hold, or any
    rating that maps to 0). A positive weight is a long target, negative a short.
    Unknown ratings are treated as Hold (the parser already defaults unmatched
    text to ``Hold``, so this is the conservative fallback).
    """
    weight = cfg.tier_target.get(rating.capitalize())
    if weight is None or weight == 0.0:
        return None
    return weight
