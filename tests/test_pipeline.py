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
import threading
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import pipeline  # noqa: E402
from scrapers._base import SearchScrapeResult, CandidateResult  # noqa: E402
from llm.match import MatchBatchResult  # noqa: E402


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


def test_a_blocked_shop_is_not_asked_again_on_the_retry():
    # A11: a shop that answered the first pass with nothing but a block gets
    # the same block on the retry (the Flipkart CAPTCHA survives a fresh page
    # load) — skip it and let the healthy lanes retry.
    url = "https://mkp.gem.gov.in/test/blocked-not-retried/p-13-13-cat.html"
    fk_calls, az_calls = [], []
    seen_events = []

    def fk(q):
        fk_calls.append(q)
        return SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[], blocked=True)

    def az(q):
        az_calls.append(q)
        return SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[])

    pipeline.run_pipeline(
        url, refresh=True, on_event=seen_events.append,
        extract_fn=_fake_gem_product, normalize_fn=lambda gp: _fake_normalized(),
        flipkart_search_fn=fk, amazon_search_fn=az,
        discover_fn=lambda q, d: [],
        generic_scrape_fn=_boom,
        match_fn=lambda n, c: MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=[]),
        expand_search_fn=lambda n, tq, td: {"new_queries": ["Logitech M650 buy"], "suggested_domains": [], "notes": ""},
    )

    assert fk_calls == ["Logitech M650 wireless mouse"]  # first pass only — no pointless retry
    assert az_calls == ["Logitech M650 wireless mouse", "Logitech M650 buy"]  # the healthy shop is still retried
    assert any(e.get("type") == "stage" and e.get("name") == "retry_flipkart_skipped" for e in seen_events)


def test_a_shop_that_only_found_nothing_is_still_retried():
    # "Blocked" is the only reason to skip — a clean empty result may just be
    # a bad query, and the retry's new phrasing may fix exactly that.
    url = "https://mkp.gem.gov.in/test/empty-still-retried/p-14-14-cat.html"
    fk_calls = []

    def fk(q):
        fk_calls.append(q)
        return SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[])

    pipeline.run_pipeline(
        url, refresh=True,
        extract_fn=_fake_gem_product, normalize_fn=lambda gp: _fake_normalized(),
        flipkart_search_fn=fk,
        amazon_search_fn=lambda q: SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[]),
        discover_fn=lambda q, d: [],
        generic_scrape_fn=_boom,
        match_fn=lambda n, c: MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=[]),
        expand_search_fn=lambda n, tq, td: {"new_queries": ["Logitech M650 buy"], "suggested_domains": [], "notes": ""},
    )

    assert fk_calls == ["Logitech M650 wireless mouse", "Logitech M650 buy"]


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
    # RELEVANT candidates must not trigger more (expensive) Selenium runs (C7).
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
                structured_fields={"title": f"Logitech M650 Wireless Mouse variant {i}", "price": 100.0 + i, "currency": "INR"},
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


def test_direct_search_junk_results_do_not_end_the_queries():
    # C7: five junk cards must not end the query loop — the right product is
    # often only found by query 2's phrasing, and the junk gets rejected by
    # the LLM anyway (at the cost of one call).
    url = "https://mkp.gem.gov.in/test/junk-does-not-stop/p-12-12-cat.html"
    fk_calls = []

    def normalize_multi(gp):
        n = _fake_normalized()
        n["search_queries"] = ["q1", "q2", "q3"]
        return n

    def _cands(prefix, titles):
        return [
            CandidateResult(
                source_domain="flipkart.com",
                url=f"https://www.flipkart.com/{prefix}/p{i}",
                scraper_used="flipkart",
                structured_fields={"title": t, "price": 100.0, "currency": "INR"},
                raw_page_text="",
            )
            for i, t in enumerate(titles)
        ]

    def fk(q):
        fk_calls.append(q)
        if q == "q1":
            return SearchScrapeResult(
                source_domain="flipkart.com", query=q,
                candidates=_cands("junk", [f"Random Gadget {i}" for i in range(6)]),
            )
        return SearchScrapeResult(
            source_domain="flipkart.com", query=q,
            candidates=_cands("item", ["Logitech M650 Wireless Mouse"] * 5),
        )

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

    assert fk_calls == ["q1", "q2"]  # q1's junk did not count; q2's relevant cards did


def test_direct_search_without_the_product_counts_every_candidate():
    # Direct callers that pass no normalized product (older tools, small
    # tests) keep the plain candidate count — their behaviour is unchanged.
    calls = []

    def search(q):
        calls.append(q)
        return SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[
            CandidateResult(
                source_domain="flipkart.com",
                url=f"https://www.flipkart.com/x/p{i}",
                scraper_used="flipkart",
                structured_fields={"title": f"Item {i}", "price": 1.0},
                raw_page_text="",
            )
            for i in range(pipeline.DIRECT_SEARCH_MIN_CANDIDATES)
        ])

    _, candidates = pipeline._search_direct(search, ["q1", "q2"])
    assert calls == ["q1"]
    assert len(candidates) == pipeline.DIRECT_SEARCH_MIN_CANDIDATES


# --- dedup uses per-site key (asin/pid/itm or host+path) ----------------


def test_dedup_flipkart_same_pid_collapses():
    c1 = CandidateResult(source_domain="flipkart.com", url="https://www.flipkart.com/mouse-a/p/itm1?pid=PID1&lid=LST1", scraper_used="flipkart", structured_fields={"title": "A", "price": 100.0}, raw_page_text="")
    c2 = CandidateResult(source_domain="flipkart.com", url="https://www.flipkart.com/mouse-b/p/itm2?pid=PID1&lid=LST2", scraper_used="flipkart", structured_fields={"title": "B", "price": 200.0}, raw_page_text="")
    assert len(pipeline._dedup([c1, c2])) == 1  # same pid -> one


def test_dedup_flipkart_diff_pid_stays_separate():
    c1 = CandidateResult(source_domain="flipkart.com", url="https://www.flipkart.com/mouse-a/p/itm1?pid=PID1", scraper_used="flipkart", structured_fields={"title": "A", "price": 100.0}, raw_page_text="")
    c2 = CandidateResult(source_domain="flipkart.com", url="https://www.flipkart.com/mouse-a/p/itm1?pid=PID2", scraper_used="flipkart", structured_fields={"title": "B", "price": 200.0}, raw_page_text="")
    assert len(pipeline._dedup([c1, c2])) == 2  # different pids -> two


def test_dedup_amazon_same_asin_collapses():
    c1 = CandidateResult(source_domain="amazon.in", url="https://www.amazon.in/dp/B0ABCDEFGH", scraper_used="amazon", structured_fields={"title": "A", "price": 100.0}, raw_page_text="")
    c2 = CandidateResult(source_domain="amazon.in", url="https://www.amazon.in/dp/B0ABCDEFGH?crid=123&sprefix=abc", scraper_used="amazon", structured_fields={"title": "B", "price": 200.0}, raw_page_text="")
    assert len(pipeline._dedup([c1, c2])) == 1  # same ASIN -> one


def test_dedup_other_shop_diff_id_stays_separate():
    c1 = CandidateResult(source_domain="brandstore.example.com", url="https://brandstore.example.com/product.php?id=1", scraper_used="generic", structured_fields={"title": "A", "price": 100.0}, raw_page_text="")
    c2 = CandidateResult(source_domain="brandstore.example.com", url="https://brandstore.example.com/product.php?id=2", scraper_used="generic", structured_fields={"title": "B", "price": 200.0}, raw_page_text="")
    assert len(pipeline._dedup([c1, c2])) == 2  # different query ids -> two


# --- A2: per-site key handles tracking params ----------------------------

def test_a2_other_shop_diff_utm_is_one():
    c1 = CandidateResult(source_domain="brandstore.example.com", url="https://brandstore.example.com/p/123?utm_source=a", scraper_used="generic", structured_fields={"title": "A", "price": 100.0}, raw_page_text="")
    c2 = CandidateResult(source_domain="brandstore.example.com", url="https://brandstore.example.com/p/123?utm_source=b", scraper_used="generic", structured_fields={"title": "B", "price": 200.0}, raw_page_text="")
    assert len(pipeline._dedup([c1, c2])) == 1  # same page, different utm -> one


def test_a2_other_shop_sorted_params_equal():
    c1 = CandidateResult(source_domain="brandstore.example.com", url="https://brandstore.example.com/A?x=1&y=2", scraper_used="generic", structured_fields={"title": "A", "price": 100.0}, raw_page_text="")
    c2 = CandidateResult(source_domain="brandstore.example.com", url="https://brandstore.example.com/A?y=2&x=1", scraper_used="generic", structured_fields={"title": "B", "price": 200.0}, raw_page_text="")
    assert len(pipeline._dedup([c1, c2])) == 1  # same params, different order -> one


def test_a2_flipkart_same_itm_diff_pid_stays_two():
    c1 = CandidateResult(source_domain="flipkart.com", url="https://www.flipkart.com/mouse/p/itm1?pid=PID1", scraper_used="flipkart", structured_fields={"title": "A", "price": 100.0}, raw_page_text="")
    c2 = CandidateResult(source_domain="flipkart.com", url="https://www.flipkart.com/mouse/p/itm1?pid=PID2", scraper_used="flipkart", structured_fields={"title": "B", "price": 200.0}, raw_page_text="")
    assert len(pipeline._dedup([c1, c2])) == 2  # different pids -> two


def test_a2_flipkart_same_pid_diff_lidq_is_one():
    c1 = CandidateResult(source_domain="flipkart.com", url="https://www.flipkart.com/mouse/p/itm1?pid=PID1&lid=LST1&q=mouse", scraper_used="flipkart", structured_fields={"title": "A", "price": 100.0}, raw_page_text="")
    c2 = CandidateResult(source_domain="flipkart.com", url="https://www.flipkart.com/mouse/p/itm1?pid=PID1&lid=LST2&q=mouse+2", scraper_used="flipkart", structured_fields={"title": "B", "price": 200.0}, raw_page_text="")
    assert len(pipeline._dedup([c1, c2])) == 1  # same pid -> one


def test_a2_amazon_dp_asin_equals_title_path_with_ref():
    c1 = CandidateResult(source_domain="amazon.in", url="https://www.amazon.in/dp/B0ABCDEFGH", scraper_used="amazon", structured_fields={"title": "A", "price": 100.0}, raw_page_text="")
    c2 = CandidateResult(source_domain="amazon.in", url="https://www.amazon.in/Title/dp/B0ABCDEFGH/ref=x?a=1", scraper_used="amazon", structured_fields={"title": "B", "price": 200.0}, raw_page_text="")
    assert len(pipeline._dedup([c1, c2])) == 1  # same ASIN -> one


# --- A3: _search_direct and _dedup share the same key function -----------

def test_a3_search_direct_and_dedup_use_same_key():
    """Both functions must produce identical keys for the same URL list."""
    urls = [
        "https://www.flipkart.com/mouse/p/itm1?pid=PID1&lid=X",
        "https://www.flipkart.com/mouse/p/itm1?pid=PID1&lid=Y",
        "https://brandstore.example.com/p/123?utm_source=a",
        "https://brandstore.example.com/p/123?utm_source=b",
        "https://www.amazon.in/dp/B0ABCDEFGH",
        "https://www.amazon.in/dp/B0ABCDEFGH?crid=123",
    ]
    from scrapers._base import CandidateResult
    candidates = [CandidateResult(source_domain="x", url=u, scraper_used="x", structured_fields={"title": "t", "price": 1.0}, raw_page_text="") for u in urls]

    deduped = pipeline._dedup(candidates)
    # Expected: flipkart same pid (1), other shop diff utm (1), amazon same ASIN (1) = 3
    assert len(deduped) == 3, f"expected 3, got {len(deduped)}"

    # Verify _search_direct produces the same dedup result
    def _fake_search(q):
        return SearchScrapeResult(source_domain="test", query=q, candidates=candidates)

    _, all_candidates = pipeline._search_direct(_fake_search, ["q1"])
    assert len(all_candidates) == 3, f"expected 3, got {len(all_candidates)}"


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


# --- merging lane results when GEM_PARALLEL=1 has each lane check its own listings -------

def _decision(url, price):
    return {"candidate_url": url, "source_domain": "shop.example", "is_match": True, "confidence": 0.9,
            "reason": "same", "price_used": price, "extraction_source": "scraper"}


def test_merge_lane_results_keeps_the_first_copy_of_a_listing():
    first = _decision("https://shop.example/p/1", 100.0)
    second = _decision("https://shop.example/p/1", 555.0)
    merged = pipeline._merge_match_results([
        MatchBatchResult(all_decisions=[first], confirmed_matches=[first]),
        MatchBatchResult(all_decisions=[second], confirmed_matches=[second]),
    ])
    assert merged.all_decisions == [first]
    assert merged.confirmed_matches == [first]


def test_merge_lane_results_sorts_matches_by_price():
    dear = _decision("https://shop.example/p/1", 900.0)
    cheap = _decision("https://shop.example/p/2", 90.0)
    merged = pipeline._merge_match_results([
        MatchBatchResult(all_decisions=[dear], confirmed_matches=[dear]),
        MatchBatchResult(all_decisions=[cheap], confirmed_matches=[cheap]),
    ])
    assert [m["price_used"] for m in merged.confirmed_matches] == [90.0, 900.0]


def test_merge_lane_results_unions_skipped_without_repeats():
    merged = pipeline._merge_match_results([
        MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=["https://shop.example/x"]),
        MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=["https://shop.example/x", "https://shop.example/y"]),
    ])
    assert merged.skipped == ["https://shop.example/x", "https://shop.example/y"]


# --- generic pages are fetched at the same time ---------------------------------------

def _discovery_two_urls(url_a, url_b):
    def discover_fn(queries, allowed_domains):
        return [{"query": queries[0], "raw_urls": [url_a, url_b], "filtered_urls": [url_a, url_b],
                 "blocked": False, "error": None}]

    return discover_fn


def test_generic_pages_are_fetched_at_the_same_time():
    url = "https://mkp.gem.gov.in/test/parallel-generic/p-13-13-cat.html"
    gate = threading.Barrier(2, timeout=2)
    fetched = []

    def generic_scrape_fn(u):
        gate.wait()
        fetched.append(u)
        return {"source_domain": u.split("/")[2], "url": u, "scraper_used": "generic",
                "structured_fields": {"title": "Logitech M650", "price": 1100.0}, "raw_page_text": "x"}

    pipeline.run_pipeline(
        url, refresh=True,
        extract_fn=_fake_gem_product, normalize_fn=lambda gp: _fake_normalized(),
        flipkart_search_fn=lambda q: SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[]),
        amazon_search_fn=lambda q: SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[]),
        discover_fn=_discovery_two_urls("https://a.example/p/1", "https://b.example/p/2"),
        generic_scrape_fn=generic_scrape_fn,
        match_fn=lambda n, c: MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=[]),
        expand_search_fn=lambda n, tq, td: {"new_queries": [], "suggested_domains": [], "notes": ""},
    )

    assert sorted(fetched) == ["https://a.example/p/1", "https://b.example/p/2"]


def test_a_broken_generic_page_does_not_stop_the_others():
    url = "https://mkp.gem.gov.in/test/parallel-generic-broken/p-14-14-cat.html"
    scraped = []

    def generic_scrape_fn(u):
        if "a.example" in u:
            raise RuntimeError("page exploded")
        scraped.append(u)
        return {"source_domain": u.split("/")[2], "url": u, "scraper_used": "generic",
                "structured_fields": {"title": "Logitech M650", "price": 1100.0}, "raw_page_text": "x"}

    pipeline.run_pipeline(
        url, refresh=True,
        extract_fn=_fake_gem_product, normalize_fn=lambda gp: _fake_normalized(),
        flipkart_search_fn=lambda q: SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[]),
        amazon_search_fn=lambda q: SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[]),
        discover_fn=_discovery_two_urls("https://a.example/p/1", "https://b.example/p/2"),
        generic_scrape_fn=generic_scrape_fn,
        match_fn=lambda n, c: MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=[]),
        expand_search_fn=lambda n, tq, td: {"new_queries": [], "suggested_domains": [], "notes": ""},
    )

    assert scraped == ["https://b.example/p/2"]


# --- one Chrome per run: started early, reused by the retry, closed -------------------

@contextmanager
def _patched_flipkart_browser(fake_class):
    """Swaps FlipkartBrowser on the (imported) flipkart module, so the
    pipeline picks the fake up without any real Chrome in the tests."""
    from scrapers import flipkart as flipkart_scraper

    had = hasattr(flipkart_scraper, "FlipkartBrowser")
    old = getattr(flipkart_scraper, "FlipkartBrowser", None)
    flipkart_scraper.FlipkartBrowser = fake_class
    try:
        yield
    finally:
        if had:
            flipkart_scraper.FlipkartBrowser = old
        else:
            del flipkart_scraper.FlipkartBrowser


def test_one_browser_runs_the_search_the_retry_and_is_closed():
    url = "https://mkp.gem.gov.in/test/one-chrome/p-60-60-cat.html"
    events = []
    browsers = []

    class FakeBrowser:
        def __init__(self):
            self.queries = []
            self.products = []
            browsers.append(self)
            events.append("created")

        def start(self):
            events.append("started")

        def search(self, query, max_results=15, max_pages=3, product=None):
            self.queries.append(query)
            self.products.append(product)
            events.append(f"search:{query}")
            return SearchScrapeResult(source_domain="flipkart.com", query=query, candidates=[])

        def close(self):
            events.append("closed")

    with _patched_flipkart_browser(FakeBrowser):
        result = pipeline.run_pipeline(
            url, refresh=True,
            extract_fn=lambda u: (events.append("extract"), _fake_gem_product(u))[1],
            normalize_fn=lambda gp: (events.append("normalize"), _fake_normalized())[1],
            # No flipkart_search_fn on purpose: the pipeline itself must bring
            # the run's one browser (None = "use the real scraper").
            flipkart_search_fn=None,
            amazon_search_fn=lambda q: SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[]),
            discover_fn=lambda q, d: [],
            generic_scrape_fn=_boom,
            match_fn=lambda n, c: MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=[]),
            expand_search_fn=lambda n, tq, td: {"new_queries": ["better query"], "suggested_domains": [], "notes": ""},
        )

    assert len(browsers) == 1
    assert events.index("started") < events.index("extract")
    assert browsers[0].queries == ["Logitech M650 wireless mouse", "better query"]
    # C6: the pipeline hands the run's browser the product's words so its page
    # loop can stop paging once page 1 already holds enough relevant cards.
    assert browsers[0].products == [{"logitech", "m650"}, {"logitech", "m650"}]
    assert events.count("closed") == 1
    assert result["matches"] == []


def test_injected_flipkart_search_function_means_no_browser_is_started():
    url = "https://mkp.gem.gov.in/test/no-browser/p-61-61-cat.html"

    class _NoBrowser:
        def __init__(self):
            raise AssertionError("a browser must not be created when a search function was injected")

    with _patched_flipkart_browser(_NoBrowser):
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
    assert result["matches"] == []


def test_the_browser_is_closed_when_a_stage_fails():
    url = "https://mkp.gem.gov.in/test/chrome-close/p-62-62-cat.html"
    events = []

    class FakeBrowser:
        def __init__(self):
            events.append("created")

        def start(self):
            events.append("started")

        def search(self, query, max_results=15, max_pages=3):
            raise AssertionError("the search must not be reached")

        def close(self):
            events.append("closed")

    def broken_extract(u):
        raise RuntimeError("gem page exploded")

    with _patched_flipkart_browser(FakeBrowser):
        try:
            pipeline.run_pipeline(
                url, refresh=True,
                extract_fn=broken_extract, normalize_fn=_boom,
                flipkart_search_fn=None, amazon_search_fn=_boom,
                discover_fn=_boom, generic_scrape_fn=_boom, match_fn=_boom, expand_search_fn=_boom,
            )
        except RuntimeError as e:
            assert "gem page exploded" in str(e)
        else:
            raise AssertionError("the extract error must reach the caller")

    assert events == ["created", "started", "closed"]


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
