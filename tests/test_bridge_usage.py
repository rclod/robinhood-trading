"""Tests for token-usage accounting + cost estimation."""

from __future__ import annotations

from types import SimpleNamespace

from bridge import usage


def _usage(prompt, completion, cached):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


def test_report_cost_and_cache_hit():
    usage.reset()
    usage._record("grok-4.3", _usage(1000, 200, 400))
    rep = usage.report()
    assert rep["total_calls"] == 1
    assert rep["cache_hit_rate"] == 0.4
    expected = (600 / 1e6 * 1.25) + (400 / 1e6 * 0.20) + (200 / 1e6 * 2.50)
    assert abs(rep["total_cost_usd"] - round(expected, 4)) < 1e-4


def test_fast_model_is_cheaper_than_flagship():
    usage.reset()
    usage._record("grok-4.3", _usage(1_000_000, 0, 0))                    # $1.25
    usage._record("grok-4-1-fast-non-reasoning", _usage(1_000_000, 0, 0))  # $0.20
    by = {r["model"]: r["cost_usd"] for r in usage.report()["by_model"]}
    assert by["grok-4.3"] > by["grok-4-1-fast-non-reasoning"]


def test_install_is_idempotent():
    class _Comp:
        def create(self, **kw):
            return SimpleNamespace(usage=_usage(10, 1, 0), model=kw.get("model"))
    mod = SimpleNamespace(Completions=_Comp)
    usage.install(mod)
    first = mod.Completions.create
    usage.install(mod)  # must not double-wrap
    assert mod.Completions.create is first
