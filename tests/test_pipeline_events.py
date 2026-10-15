"""run_pipeline should narrate what it is doing, in order, through on_event."""
import json

from core import pipeline
from llm.match import MatchBatchResult
from scrapers._base import CandidateResult, SearchScrapeResult

GEM_URL = "https://mkp.gem.gov.in/computer-mouse/demo-mouse/p-300-400-cat.html"


def _gem_product(url):
    return {
        "url": url, "category_slug": "computer-mouse", "product_slug": "demo-mouse",
        "title": "Demo Wireless Mouse", "brand": "Demo", "model": None, "specs": {},
        "gem_price": 1000.0, "gem_price_type": "fixed", "currency": "INR",
    }


def _normalized(_gem):
    return {
        "canonical_name": "Demo Mouse", "brand": "Demo", "model": None, "key_identifiers": ["Demo"],
        "search_queries": ["demo wireless mouse"], "gem_price": 1000.0,
    }


def _candidate(domain, url, title, price):
    return CandidateResult(
        source_domain=domain, url=url, scraper_used=domain.split(".")[0],
        structured_fields={"title": title, "price": price, "currency": "INR"}, raw_page_text="",
    )


def _search(domain, candidates):
    return lambda query: SearchScrapeResult(source_domain=domain, query=query, candidates=list(candidates))


def _match_all(_normalized_product, candidates):
    decisions = [
        {"candidate_url": c.url, "source_domain": c.source_domain, "is_match": True, "confidence": 0.9,
         "reason": "same", "price_used": c.structured_fields["price"], "extraction_source": "scraper"}
        for c in candidates
    ]
    return MatchBatchResult(all_decisions=decisions, confirmed_matches=list(decisions))


def _no_expansion(*_args):
    return {"new_queries": [], "suggested_domains": [], "notes": ""}


def _run(url=GEM_URL, **overrides):
    seen = []
    kwargs = dict(
        extract_fn=_gem_product,
        normalize_fn=_normalized,
        flipkart_search_fn=_search("flipkart.com", [_candidate("flipkart.com", "https://www.flipkart.com/a", "Demo Mouse A", 900.0)]),
        amazon_search_fn=_search("amazon.in", [_candidate("amazon.in", "https://www.amazon.in/b", "Demo Mouse B", 950.0)]),
        discover_fn=lambda queries, domains: [],
        generic_scrape_fn=lambda u: None,
        match_fn=_match_all,
        expand_search_fn=_no_expansion,
    )
    kwargs.update(overrides)
    result = pipeline.run_pipeline(url, on_event=seen.append, **kwargs)
    return result, seen


def _types(seen):
    return [e["type"] for e in seen]


def test_a_normal_run_reports_every_step_in_order():
    result, seen = _run()
    types = _types(seen)

    assert types[0] == "run_started"
    assert types[-1] == "run_finished"
    order = ["gem_product", "normalized", "search_started", "search_finished", "candidates_total"]
    positions = [types.index(t) for t in order]
    assert positions == sorted(positions)
    assert [e["name"] for e in seen if e["type"] == "stage"][:4] == ["extract", "normalize", "search_flipkart", "search_amazon"]


def test_gem_product_and_search_details_are_in_the_events():
    _, seen = _run()
    gem = next(e for e in seen if e["type"] == "gem_product")
    assert gem["title"] == "Demo Wireless Mouse" and gem["price"] == 1000.0

    finished = [e for e in seen if e["type"] == "search_finished"]
    assert {e["marketplace"] for e in finished} == {"flipkart", "amazon"}
    flipkart = next(e for e in finished if e["marketplace"] == "flipkart")
    assert flipkart["count"] == 1
    assert flipkart["candidates"][0]["title"] == "Demo Mouse A"
    assert flipkart["candidates"][0]["price"] == 900.0


def test_run_finished_carries_the_headline_numbers():
    result, seen = _run()
    done = seen[-1]
    assert done["matches"] == 2
    assert done["cheapest"] == 900.0
    assert done["savings"] == 100.0
    assert done["trace_id"] == result["trace_id"]
    assert done["from_cache"] is False


def test_a_cache_hit_says_so_and_still_finishes():
    _run(url=GEM_URL + "?cache-test")
    _, seen = _run(url=GEM_URL + "?cache-test")

    types = _types(seen)
    assert types == ["run_started", "cache_hit", "run_finished"]
    assert seen[-1]["from_cache"] is True


def test_the_retry_is_announced():
    def no_match(_n, _c):
        return MatchBatchResult()

    def expansion(*_args):
        return {"new_queries": ["demo mouse wireless black"], "suggested_domains": [], "notes": "try colour"}

    _, seen = _run(url=GEM_URL + "?retry-test", match_fn=no_match, expand_search_fn=expansion)
    names = [e["name"] for e in seen if e["type"] == "stage"]
    assert "retry" in names
    plan = next(e for e in seen if e["type"] == "retry_plan")
    assert plan["new_queries"] == ["demo mouse wireless black"]


def test_the_events_are_saved_inside_the_trace():
    result, _ = _run(url=GEM_URL + "?trace-test")
    trace = json.loads((pipeline.TRACES_DIR / f"{result['trace_id']}.json").read_text(encoding="utf-8"))
    types = [e["type"] for e in trace["events"]]
    assert types[0] == "run_started" and types[-1] == "run_finished"


def test_running_without_a_listener_still_works():
    result = pipeline.run_pipeline(
        GEM_URL + "?silent",
        extract_fn=_gem_product, normalize_fn=_normalized,
        flipkart_search_fn=_search("flipkart.com", []), amazon_search_fn=_search("amazon.in", []),
        discover_fn=lambda q, d: [], generic_scrape_fn=lambda u: None,
        match_fn=lambda n, c: MatchBatchResult(), expand_search_fn=_no_expansion,
    )
    assert result["matches"] == []
