"""Tests for matching/llm_attributes.py — cache behavior and fallback
correctness. Deliberately does NOT call the real NVIDIA API (no network,
no API key needed to run these)."""
import json
import sqlite3

import pytest

from matching.llm_attributes import (
    get_product_attributes, _ensure_cache_table, _save_cache, _get_cached,
    _regex_fallback, _extract_json, FALLBACK_SOURCE,
)


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


def test_cache_miss_then_hit_returns_same_value(con, monkeypatch):
    # Force the "LLM unavailable" path so this test needs no network/key.
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    link = "https://mkp.gem.gov.in/some-product/p-123-456-cat.html"
    first = get_product_attributes("Some Product", link, con)
    assert first["source"] == FALLBACK_SOURCE

    # Second call must be a pure cache read — monkeypatch _call_llm to
    # raise if it's ever invoked again, proving no second attempt is made.
    import matching.llm_attributes as mod
    monkeypatch.setattr(mod, "_call_llm", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("cache was bypassed — _call_llm should not run twice")))
    second = get_product_attributes("Some Product", link, con)
    assert second == first


def test_regex_fallback_never_crashes_on_empty_link():
    result = _regex_fallback("Just A Name With No Slug", "")
    assert result["source"] == FALLBACK_SOURCE
    assert isinstance(result["search_query"], str)


def test_cache_round_trip_preserves_key_specs_list(con):
    _ensure_cache_table(con)
    attrs = {"brand": "HP", "model": "115", "key_specs": ["Wired", "Optical"],
              "search_query": "HP 115 wired mouse", "source": "llm"}
    _save_cache(con, "https://example.com/p-1-cat.html", attrs)
    got = _get_cached(con, "https://example.com/p-1-cat.html")
    assert got["key_specs"] == ["Wired", "Optical"]
    assert got["brand"] == "HP"


def test_markdown_fence_stripping_regex():
    import re
    raw = '```json\n{"brand": "HP"}\n```'
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    parsed = json.loads(cleaned)
    assert parsed["brand"] == "HP"


def test_extract_json_handles_reasoning_noise_and_truncation(con):
    # Reasoning models (gpt-oss-20b) emit a thinking trace before the object.
    noisy = ('We need to output brand, model, specs.\n'
             'Thinking about "fingers superhit":\n'
             '{"brand": "Fingers", "model": "Superhit", '
             '"key_specs": ["Wired", "Mouse"], '
             '"search_query": "fingers superhit wired mouse"}')
    assert _extract_json(noisy)["brand"] == "Fingers"

    # Truncated/broken JSON must degrade to None, not raise.
    broken = '{"brand": "Lapcare", "model": "LMK 012", "search_query": "Lapc'
    assert _extract_json(broken) is None

    # A stray trailing code fence after a valid object.
    tainted = '{"brand": "HP"}\n```'
    assert _extract_json(tainted)["brand"] == "HP"


def test_extract_json_empty_input_is_none():
    assert _extract_json("") is None
    assert _extract_json("not json at all") is None