"""The on/off switches: parallel search, retry, and the two gates."""
import threading

import pytest

from core import config, pipeline
from llm import match as matcher
from llm.match import MatchBatchResult
from scrapers._base import CandidateResult, SearchScrapeResult

GEM_URL = "https://mkp.gem.gov.in/computer-mouse/demo-mouse/p-300-400-cat.html"


def _gem_product(url):
    return {"url": url, "category_slug": "computer-mouse", "product_slug": "demo-mouse", "title": "Demo Wireless Mouse",
            "brand": "Demo", "model": None, "specs": {}, "gem_price": 1000.0, "gem_price_type": "fixed", "currency": "INR"}


def _normalized(_gem):
    return {"canonical_name": "Demo Mouse", "brand": "Demo", "model": None, "key_identifiers": ["Demo"],
            "search_queries": ["demo wireless mouse"], "gem_price": 1000.0}


def _cand(domain, url, price):
    return CandidateResult(source_domain=domain, url=url, scraper_used=domain.split(".")[0],
                           structured_fields={"title": "Demo Mouse", "price": price, "currency": "INR"}, raw_page_text="")


def _search(domain, candidates, hook=None):
    def fn(query):
        if hook:
            hook()
        return SearchScrapeResult(source_domain=domain, query=query, candidates=list(candidates))
    return fn


def _nothing_matches(_n, candidates):
    return MatchBatchResult(all_decisions=[], confirmed_matches=[])


def _one_match(_n, candidates):
    d = {"candidate_url": candidates[0].url, "source_domain": candidates[0].source_domain, "is_match": True,
         "confidence": 0.9, "reason": "same", "price_used": 900.0, "extraction_source": "scraper"}
    return MatchBatchResult(all_decisions=[d], confirmed_matches=[d])


def _run(**overrides):
    seen = []
    kwargs = dict(
        extract_fn=_gem_product, normalize_fn=_normalized,
        flipkart_search_fn=_search("flipkart.com", [_cand("flipkart.com", "https://www.flipkart.com/a", 900.0)]),
        amazon_search_fn=_search("amazon.in", [_cand("amazon.in", "https://www.amazon.in/dp/B0ABCDEFGH", 950.0)]),
        discover_fn=lambda q, d: [], generic_scrape_fn=lambda u: None,
        match_fn=_one_match,
        expand_search_fn=lambda *a: {"new_queries": ["better words"], "suggested_domains": [], "notes": ""},
    )
    kwargs.update(overrides)
    result = pipeline.run_pipeline(GEM_URL, on_event=seen.append, **kwargs)
    return result, seen


# --- reading the settings ------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("no", False)])
def test_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("SOME_FLAG", raw)
    assert config._flag("SOME_FLAG", not expected) is expected


def test_flag_empty_means_default(monkeypatch):
    monkeypatch.setenv("SOME_FLAG", "")
    assert config._flag("SOME_FLAG", True) is True  # empty → default True
    assert config._flag("SOME_FLAG", False) is False  # empty → default False


def test_flag_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_FLAG", raising=False)
    assert config._flag("SOME_FLAG", True) is True and config._flag("SOME_FLAG", False) is False


def test_normal_behaviour_is_the_default():
    # these tests assume the developer has not exported the switches
    assert config.RETRY_ENABLED is True and config.GATES_ENABLED is True
    assert config.PARALLEL_SEARCH is False and config.LLM_CACHE_ENABLED is False


# --- parallel search ------------------------------------------------------------

def test_sequential_search_keeps_the_old_stage_names():
    _, seen = _run()
    assert [e["name"] for e in seen if e["type"] == "stage"][2:4] == ["search_flipkart", "search_amazon"]


def test_parallel_search_runs_both_at_once_and_events_survive(monkeypatch):
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", True)
    gate = threading.Barrier(2, timeout=5)
    result, seen = _run(
        flipkart_search_fn=_search("flipkart.com", [_cand("flipkart.com", "https://www.flipkart.com/a", 900.0)], hook=gate.wait),
        amazon_search_fn=_search("amazon.in", [_cand("amazon.in", "https://www.amazon.in/dp/B0ABCDEFGH", 950.0)], hook=gate.wait),
    )
    names = [e["name"] for e in seen if e["type"] == "stage"]
    assert "search_both" in names and "search_flipkart" not in names
    finished = {e["marketplace"] for e in seen if e["type"] == "search_finished"}
    assert finished == {"flipkart", "amazon"}
    assert result["matches"]


def test_parallel_search_gives_the_same_answer_as_sequential(monkeypatch):
    sequential, _ = _run()
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", True)
    parallel, _ = _run()
    assert parallel["matches"] == sequential["matches"]
    assert parallel["not_found_on"] == sequential["not_found_on"]


# --- retry switch ---------------------------------------------------------------

def test_retry_happens_by_default_when_nothing_matched():
    _, seen = _run(match_fn=_nothing_matches)
    assert any(e["type"] == "retry_plan" for e in seen)


def test_retry_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(pipeline, "RETRY_ENABLED", False)
    result, seen = _run(match_fn=_nothing_matches)
    assert not any(e["type"] == "retry_plan" for e in seen)
    assert result["matches"] == []


# --- gates switch ---------------------------------------------------------------

def _fake_verify(price, currency="INR"):
    def fake(normalized, candidate, client=None):
        return {"candidate_url": candidate["url"], "source_domain": candidate.get("source_domain"), "is_match": True,
                "confidence": 0.95, "reason": "looks the same", "price_used": price, "extraction_source": "scraper"}
    return fake


def _candidate_dict(currency="INR"):
    return {"url": "https://shop.example/p/1", "source_domain": "shop.example",
            "structured_fields": {"title": "Demo Mouse", "price": 39.0, "currency": currency}, "raw_page_text": "x"}


def test_price_gate_rejects_a_wildly_cheap_listing_by_default(monkeypatch):
    monkeypatch.setattr(matcher, "verify_or_extract", _fake_verify(39.0))
    out = matcher.match_candidates({"gem_price": 1000.0}, [_candidate_dict()])
    assert out.confirmed_matches == []


def test_price_gate_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(matcher, "verify_or_extract", _fake_verify(39.0))
    monkeypatch.setattr(matcher, "GATES_ENABLED", False)
    out = matcher.match_candidates({"gem_price": 1000.0}, [_candidate_dict()])
    assert len(out.confirmed_matches) == 1


def test_currency_gate_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(matcher, "verify_or_extract", _fake_verify(1000.0))
    assert matcher.match_candidates({"gem_price": 1000.0}, [_candidate_dict("USD")]).confirmed_matches == []
    monkeypatch.setattr(matcher, "GATES_ENABLED", False)
    assert len(matcher.match_candidates({"gem_price": 1000.0}, [_candidate_dict("USD")]).confirmed_matches) == 1
