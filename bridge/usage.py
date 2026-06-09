"""Per-run LLM token-usage + cost logging.

propagate's LLM calls happen inside TradingAgents (kept pristine), so the bridge
captures usage by monkey-patching the OpenAI SDK's chat-completions call (the
same approach as the macro-news cache). Each call's ``usage`` is accumulated per
model — including ``cached_tokens`` so we can see the prompt-cache benefit — and
:func:`report` applies the configured Grok rates to estimate cost.

Rates are approximate (xAI mid-2026, USD per 1M tokens) and overridable; the
authoritative figure is always the xAI console. The point here is *relative*
visibility: calls, tokens, cache-hit %, and a cost estimate per run.
"""

from __future__ import annotations

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

# model substring -> (input, cached_input, output) USD per 1M tokens
RATES = {
    "grok-4.3": (1.25, 0.20, 2.50),
    "grok-4-1-fast": (0.20, 0.05, 0.50),
}
_DEFAULT_RATE = (1.25, 0.20, 2.50)  # assume flagship if unknown

_acc: dict = defaultdict(lambda: {"calls": 0, "prompt": 0, "cached": 0, "completion": 0})


def reset() -> None:
    _acc.clear()


def _record(model, usage) -> None:
    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
    a = _acc[model or "?"]
    a["calls"] += 1
    a["prompt"] += getattr(usage, "prompt_tokens", 0) or 0
    a["cached"] += cached
    a["completion"] += getattr(usage, "completion_tokens", 0) or 0


def install(cc_module=None) -> None:
    """Idempotently patch openai chat-completions create to record usage."""
    if cc_module is None:
        try:
            import openai.resources.chat.completions as cc_module  # type: ignore
        except Exception as exc:  # pragma: no cover
            logger.debug("usage logger not installed: %s", exc)
            return

    orig = cc_module.Completions.create
    if getattr(orig, "_bridge_usage", False):
        return

    def wrapped(self, *args, **kwargs):
        r = orig(self, *args, **kwargs)
        try:
            u = getattr(r, "usage", None)
            if u is not None:
                _record(kwargs.get("model") or getattr(r, "model", "?"), u)
        except Exception:  # never let logging break a real call
            pass
        return r

    wrapped._bridge_usage = True
    cc_module.Completions.create = wrapped


def _rate(model: str):
    for key, val in RATES.items():
        if key in model:
            return val
    return _DEFAULT_RATE


def report() -> dict:
    """Per-model + total usage and estimated cost for the calls seen so far."""
    rows, total_cost, total_calls, total_tokens, total_cached = [], 0.0, 0, 0, 0
    for model, a in sorted(_acc.items()):
        ir, cr, orate = _rate(model)
        uncached = max(0, a["prompt"] - a["cached"])
        cost = uncached / 1e6 * ir + a["cached"] / 1e6 * cr + a["completion"] / 1e6 * orate
        rows.append({"model": model, **a, "cost_usd": round(cost, 4)})
        total_cost += cost
        total_calls += a["calls"]
        total_tokens += a["prompt"] + a["completion"]
        total_cached += a["cached"]
    cache_hit = (total_cached / sum(a["prompt"] for a in _acc.values())) if _acc else 0.0
    return {
        "by_model": rows,
        "total_calls": total_calls,
        "total_tokens": total_tokens,
        "cache_hit_rate": round(cache_hit, 3),
        "total_cost_usd": round(total_cost, 4),
    }
