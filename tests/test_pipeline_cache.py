"""Caching rules for run_pipeline.

A run that came back empty only because a marketplace blocked us should not
be replayed from the cache for the next hour.
"""
from core import pipeline
from llm.match import MatchBatchResult
from scrapers._base import SearchScrapeResult

GEM_URL = "https://mkp.gem.gov.in/computer-mouse/demo-mouse/p-100-200-cat.html"


def _gem_product(url):
    return {
        "url": url,
        "category_slug": "computer-mouse",
        "product_slug": "demo-mouse",
        "title": "Demo Wireless Mouse",
        "brand": "Demo",
        "model": None,
        "specs": {},
        "gem_price": 1000.0,
        "gem_price_type": "fixed",
        "currency": "INR",
    }


def _normalized(_gem_product_dict):
    return {
        "canonical_name": "Demo Mouse",
        "brand": "Demo",
        "model": None,
        "key_identifiers": ["Demo"],
        "search_queries": ["demo wireless mouse"],
        "gem_price": 1000.0,
    }


def _no_matches(_normalized_product, _candidates):
    return MatchBatchResult()


def _no_expansion(*_args):
    return {"new_queries": [], "suggested_domains": [], "notes": ""}


def _clean_search(domain):
    return lambda query: SearchScrapeResult(source_domain=domain, query=query, candidates=[])


def _blocked_search(domain):
    return lambda query: SearchScrapeResult(source_domain=domain, query=query, candidates=[], blocked=True)


def _run(make_search, extract_calls):
    def extract(url):
        extract_calls.append(url)
        return _gem_product(url)

    return pipeline.run_pipeline(
        GEM_URL,
        extract_fn=extract,
        normalize_fn=_normalized,
        flipkart_search_fn=make_search("flipkart.com"),
        amazon_search_fn=make_search("amazon.in"),
        discover_fn=lambda queries, domains: [],
        generic_scrape_fn=lambda url: None,
        match_fn=_no_matches,
        expand_search_fn=_no_expansion,
    )


def test_clean_empty_run_is_cached():
    calls = []
    _run(_clean_search, calls)
    second = _run(_clean_search, calls)

    assert second["from_cache"] is True
    assert len(calls) == 1


def test_blocked_run_is_not_cached():
    calls = []
    first = _run(_blocked_search, calls)
    assert first["search_issues"]  # the blocks were reported

    second = _run(_blocked_search, calls)
    assert second["from_cache"] is False
    assert len(calls) == 2  # the second run really ran again
