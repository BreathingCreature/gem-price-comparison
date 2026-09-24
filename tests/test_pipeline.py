"""
Tests for pipeline.py.

Every stage (extract, normalize, flipkart/amazon search, discover, generic
scrape, match, expand_search) is injected as a fake via run_pipeline's
keyword overrides. This tests ONLY the orchestration logic — sequencing,
the retry trigger, cache behavior, and not_found_on computation — not
whether the real scrapers/LLM calls work (those are already covered by
each part's own test file). Each test uses a distinct fake gem_url so
tests don't collide through the real on-disk cache.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline  # noqa: E402
from scrapers.base import SearchScrapeResult, CandidateResult  # noqa: E402
from matcher import MatchBatchResult  # noqa: E402


def _fake_gem_product(url):
    return {
        "url": url,
        "category_slug": "computer-mouse",
        "product_slug": "logitech-m650",
        "title": "Logitech M650 Wireless Mouse",
        "brand": "Logitech",
        "model": None,
        "specs": {},
        "gem_price": 1299.0,
        "gem_price_type": "fixed",
        "currency": "INR",
    }


def _fake_normalized():
    return {
        "canonical_name": "Logitech M650",
        "brand": "Logitech",
        "model": "M650",
        "key_identifiers": ["M650"],
        "search_queries": ["Logitech M650 wireless mouse"],
    }


def _boom(*args, **kwargs):
    raise AssertionError("this stage should not have been called")


# --- cache ---------------------------------------------------------------------------

def test_cache_hit_skips_every_stage():
    url = "https://mkp.gem.gov.in/test/cache-hit/p-1-1-cat.html"
    pipeline._cache_set(
        url,
        {"gem_product": {}, "matches": [{"source_domain": "flipkart.com", "price_used": 999}], "not_found_on": [], "from_cache": False, "trace_id": "abc123"},
    )

    result = pipeline.run_pipeline(
        url, refresh=False,
        extract_fn=_boom, normalize_fn=_boom, flipkart_search_fn=_boom, amazon_search_fn=_boom,
        discover_fn=_boom, generic_scrape_fn=_boom, match_fn=_boom, expand_search_fn=_boom,
    )

    assert result["from_cache"] is True
    assert result["trace_id"] == "abc123"


def test_refresh_true_bypasses_cache():
    url = "https://mkp.gem.gov.in/test/refresh-bypass/p-2-2-cat.html"
    pipeline._cache_set(url, {"gem_product": {}, "matches": [], "not_found_on": [], "from_cache": False, "trace_id": "stale"})

    result = pipeline.run_pipeline(
        url, refresh=True,
        extract_fn=_fake_gem_product, normalize_fn=lambda gp: _fake_normalized(),
        flipkart_search_fn=lambda q: SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[]),
        amazon_search_fn=lambda q: SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[]),
        discover_fn=lambda q, d: [],
        generic_scrape_fn=_boom,
        match_fn=lambda n, c: MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=[]),
        expand_search_fn=lambda n, tq, td: {"new_queries": [], "suggested_domains": [], "notes": ""},
    )
    assert result["trace_id"] != "stale"


def test_cache_expired_entry_is_a_miss():
    url = "https://mkp.gem.gov.in/test/cache-expired/p-3-3-cat.html"
    path = pipeline._cache_path(url)
    path.write_text(json.dumps({"cached_at": time.time() - 999_999, "result": {"trace_id": "old"}}), encoding="utf-8")
    assert pipeline._cache_get(url) is None


# --- happy path, no retry needed ------------------------------------------------------

def test_happy_path_flipkart_match_amazon_not_found():
    url = "https://mkp.gem.gov.in/test/happy-path/p-4-4-cat.html"

    def flipkart_search_fn(q):
        return SearchScrapeResult(
            source_domain="flipkart.com",
            query=q,
            candidates=[
                CandidateResult(
                    source_domain="flipkart.com",
                    url="https://www.flipkart.com/m650/p/itm1",
                    scraper_used="flipkart",
                    structured_fields={"title": "Logitech M650", "price": 1199.0},
                    raw_page_text="",
                )
            ],
        )

    def match_fn(normalized, candidates):
        decision = {
            "candidate_url": "https://www.flipkart.com/m650/p/itm1",
            "source_domain": "flipkart.com",
            "is_match": True,
            "confidence": 0.9,
            "reason": "same brand and model",
            "price_used": 1199.0,
            "extraction_source": "scraper",
        }
        return MatchBatchResult(all_decisions=[decision], confirmed_matches=[decision], skipped=[])

    result = pipeline.run_pipeline(
        url, refresh=True,
        extract_fn=_fake_gem_product, normalize_fn=lambda gp: _fake_normalized(),
        flipkart_search_fn=flipkart_search_fn,
        amazon_search_fn=lambda q: SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[]),
        discover_fn=lambda q, d: [],
        generic_scrape_fn=_boom,  # no discovery URLs -> should never be called
        match_fn=match_fn,
        expand_search_fn=_boom,  # a match was found -> retry/expand should never fire
    )

    assert result["from_cache"] is False
    assert len(result["matches"]) == 1
    assert result["matches"][0]["price_used"] == 1199.0
    assert "amazon.in" in result["not_found_on"]
    assert "flipkart.com" not in result["not_found_on"]
    assert result["trace_id"]


# --- retry behaviour -------------------------------------------------------------------

def test_retry_triggers_on_zero_matches_and_finds_one_via_expanded_discovery():
    url = "https://mkp.gem.gov.in/test/retry-success/p-5-5-cat.html"
    call_count = {"match": 0}

    def discover_fn(queries, allowed_domains):
        if allowed_domains == ["croma.com"]:
            return [{"query": queries[0], "raw_urls": ["https://www.croma.com/m650"], "filtered_urls": ["https://www.croma.com/m650"], "blocked": False, "error": None}]
        return []

    def generic_scrape_fn(u):
        return {"source_domain": "croma.com", "url": u, "scraper_used": "generic", "structured_fields": {"title": "Logitech M650", "price": 1399.0}, "raw_page_text": ""}

    def match_fn(normalized, candidates):
        call_count["match"] += 1
        if not candidates:
            return MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=[])
        decision = {
            "candidate_url": candidates[0]["url"],
            "source_domain": "croma.com",
            "is_match": True,
            "confidence": 0.88,
            "reason": "matched via expanded discovery",
            "price_used": 1399.0,
            "extraction_source": "scraper",
        }
        return MatchBatchResult(all_decisions=[decision], confirmed_matches=[decision], skipped=[])

    def expand_search_fn(normalized, tried_queries, tried_domains):
        assert "flipkart.com" in tried_domains and "amazon.in" in tried_domains
        return {"new_queries": ["Logitech M650 mouse buy online"], "suggested_domains": ["croma.com"], "notes": "Croma commonly stocks Logitech."}

    result = pipeline.run_pipeline(
        url, refresh=True,
        extract_fn=_fake_gem_product, normalize_fn=lambda gp: _fake_normalized(),
        flipkart_search_fn=lambda q: SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[]),
        amazon_search_fn=lambda q: SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[]),
        discover_fn=discover_fn,
        generic_scrape_fn=generic_scrape_fn,
        match_fn=match_fn,
        expand_search_fn=expand_search_fn,
    )

    assert call_count["match"] == 2  # empty first pass + successful retry pass
    assert len(result["matches"]) == 1
    assert result["matches"][0]["source_domain"] == "croma.com"


def test_retry_skipped_when_expand_search_has_no_new_queries():
    url = "https://mkp.gem.gov.in/test/retry-skipped/p-6-6-cat.html"

    def expand_search_fn(normalized, tried_queries, tried_domains):
        return {"new_queries": [], "suggested_domains": [], "notes": "No confident suggestions."}

    result = pipeline.run_pipeline(
        url, refresh=True,
        extract_fn=_fake_gem_product, normalize_fn=lambda gp: _fake_normalized(),
        flipkart_search_fn=lambda q: SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[]),
        amazon_search_fn=lambda q: SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[]),
        discover_fn=lambda q, d: [],
        generic_scrape_fn=_boom,  # no queries to expand into -> should never be called
        match_fn=lambda n, c: MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=[]),
        expand_search_fn=expand_search_fn,
    )

    assert result["matches"] == []
    assert "flipkart.com" in result["not_found_on"]
    assert "amazon.in" in result["not_found_on"]


# --- honesty: skipped vs not_found ------------------------------------------------------

def test_skipped_domain_not_reported_as_not_found():
    # Amazon search succeeded but EVERY candidate's verify was skipped
    # (LLM outage). Previously the domain was listed under "Not found"
    # — a verify failure dressed up as a shopping verdict.
    url = "https://mkp.gem.gov.in/test/skipped-not-notfound/p-7-7-cat.html"

    def amazon_search_fn(q):
        return SearchScrapeResult(
            source_domain="amazon.in",
            query=q,
            candidates=[
                CandidateResult(
                    source_domain="amazon.in",
                    url="https://www.amazon.in/dp/B0SKIP",
                    scraper_used="amazon",
                    structured_fields={"title": "x", "price": 1.0, "currency": "INR"},
                    raw_page_text="x",
                )
            ],
        )

    def match_fn(normalized, candidates):
        return MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=["https://www.amazon.in/dp/B0SKIP"])

    result = pipeline.run_pipeline(
        url, refresh=True,
        extract_fn=_fake_gem_product, normalize_fn=lambda gp: _fake_normalized(),
        flipkart_search_fn=lambda q: SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[]),
        amazon_search_fn=amazon_search_fn,
        discover_fn=lambda q, d: [],
        generic_scrape_fn=_boom,
        match_fn=match_fn,
        expand_search_fn=lambda n, tq, td: {"new_queries": [], "suggested_domains": [], "notes": ""},
    )

    assert "amazon.in" not in result["not_found_on"]
    assert "flipkart.com" in result["not_found_on"]  # clean empty search still honestly not-found
    assert any("amazon.in" in i and "skipped" in i for i in result["search_issues"])


def test_gem_price_missing_emits_warning():
    url = "https://mkp.gem.gov.in/test/no-gem-price-warning/p-9-9-cat.html"

    def extract_no_price(u):
        p = _fake_gem_product(u)
        p["gem_price"] = None
        p["gem_price_type"] = "unknown"
        return p

    result = pipeline.run_pipeline(
        url, refresh=True,
        extract_fn=extract_no_price, normalize_fn=lambda gp: _fake_normalized(),
        flipkart_search_fn=lambda q: SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[]),
        amazon_search_fn=lambda q: SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[]),
        discover_fn=lambda q, d: [],
        generic_scrape_fn=_boom,
        match_fn=lambda n, c: MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=[]),
        expand_search_fn=lambda n, tq, td: {"new_queries": [], "suggested_domains": [], "notes": ""},
    )

    assert any("sanity gate" in w.lower() for w in result["warnings"])


# --- secondary normalized queries reach direct scrapers -----------------------------------

def test_all_normalized_queries_reach_direct_scrapers():
    # Previously only search_queries[0] hit Amazon/Flipkart — a bad primary
    # phrasing meant the two working marketplaces were searched once, wrong.
    url = "https://mkp.gem.gov.in/test/all-queries-direct/p-10-10-cat.html"
    seen = {"flipkart": [], "amazon": []}

    def normalize_multi(gp):
        n = _fake_normalized()
        n["search_queries"] = ["query one", "query two", "query three"]
        return n

    def fk(q):
        seen["flipkart"].append(q)
        return SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[])

    def az(q):
        seen["amazon"].append(q)
        return SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[])

    pipeline.run_pipeline(
        url, refresh=True,
        extract_fn=_fake_gem_product, normalize_fn=normalize_multi,
        flipkart_search_fn=fk, amazon_search_fn=az,
        discover_fn=lambda q, d: [],
        generic_scrape_fn=_boom,
        match_fn=lambda n, c: MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=[]),
        expand_search_fn=lambda n, tq, td: {"new_queries": [], "suggested_domains": [], "notes": ""},
    )

    assert seen["flipkart"] == ["query one", "query two", "query three"]
    assert seen["amazon"] == ["query one", "query two", "query three"]


def test_direct_search_stops_once_enough_candidates():
    # Early-exit: a query that already returned >= DIRECT_SEARCH_MIN_CANDIDATES
    # candidates must not trigger more (expensive) Selenium runs.
    url = "https://mkp.gem.gov.in/test/stop-early/p-11-11-cat.html"
    fk_calls = []

    def normalize_multi(gp):
        n = _fake_normalized()
        n["search_queries"] = ["q1", "q2", "q3"]
        return n

    def fk(q):
        fk_calls.append(q)
        cands = [
            CandidateResult(
                source_domain="flipkart.com",
                url=f"https://www.flipkart.com/item/p{i}",
                scraper_used="flipkart",
                structured_fields={"title": f"Item {i}", "price": 100.0 + i, "currency": "INR"},
                raw_page_text="",
            )
            for i in range(pipeline.DIRECT_SEARCH_MIN_CANDIDATES)
        ]
        return SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=cands)

    pipeline.run_pipeline(
        url, refresh=True,
        extract_fn=_fake_gem_product, normalize_fn=normalize_multi,
        flipkart_search_fn=fk,
        amazon_search_fn=lambda q: SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[]),
        discover_fn=lambda q, d: [],
        generic_scrape_fn=_boom,
        match_fn=lambda n, c: MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=[]),
        expand_search_fn=lambda n, tq, td: {"new_queries": [], "suggested_domains": [], "notes": ""},
    )

    assert fk_calls == ["q1"]  # enough after first query — q2/q3 never searched


def test_retry_filters_non_indian_suggested_domains():
    # LLM prompt asks for Indian domains but doesn't enforce it —
    # bestbuy.com must not reopen open-mode .com discovery.
    url = "https://mkp.gem.gov.in/test/retry-indian-filter/p-12-12-cat.html"
    discover_calls = []

    def discover_fn(queries, allowed_domains):
        discover_calls.append(allowed_domains)
        return []

    def expand_search_fn(normalized, tried_queries, tried_domains):
        return {
            "new_queries": ["alternate phrasing"],
            "suggested_domains": ["bestbuy.com", "reliancedigital.in"],
            "notes": "test",
        }

    pipeline.run_pipeline(
        url, refresh=True,
        extract_fn=_fake_gem_product, normalize_fn=lambda gp: _fake_normalized(),
        flipkart_search_fn=lambda q: SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[]),
        amazon_search_fn=lambda q: SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[]),
        discover_fn=discover_fn,
        generic_scrape_fn=lambda u: {"source_domain": "reliancedigital.in", "url": u, "scraper_used": "generic", "structured_fields": None, "raw_page_text": "x"},
        match_fn=lambda n, c: MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=[]),
        expand_search_fn=expand_search_fn,
    )

    assert discover_calls[0] is None  # first pass: open discovery
    assert discover_calls[-1] == ["reliancedigital.in"]  # retry: Indian suggestion kept, bestbuy dropped


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
