"""Tests for the speculative scanner parsing + candidate loading."""

from __future__ import annotations

import json

from bridge.scanner import _extract_json, load_speculative, speculative_tickers


def test_extract_json_pulls_the_array_after_prose():
    txt = ('Here is my reasoning about the news cycle...\n'
           '[{"ticker":"ASTS","conviction":70},{"ticker":"ONDS","conviction":60}]')
    out = _extract_json(txt)
    assert [c["ticker"] for c in out] == ["ASTS", "ONDS"]


def test_extract_json_returns_empty_on_garbage():
    assert _extract_json("no json here") == []


def test_load_speculative_and_tickers(tmp_path):
    p = tmp_path / "speculative.json"
    p.write_text(json.dumps({"candidates": [
        {"ticker": "asts", "conviction": 70},
        {"ticker": "onds", "conviction": 60},
        {"company": "no ticker"},
    ]}))
    assert speculative_tickers(str(p)) == ["ASTS", "ONDS"]
    assert len(load_speculative(str(p))) == 3


def test_load_speculative_missing_file_is_empty():
    assert load_speculative("/nonexistent/spec.json") == []
