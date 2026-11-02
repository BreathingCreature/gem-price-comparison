"""Characterization: event order and result v2 stay byte-for-byte the same.

The demo golden files come from the Task 0 baseline. The three fake-shop
scenarios pin the sequential path and record the same two things.
"""
import json
from pathlib import Path

from api import demo
from core import pipeline
from llm.match import MatchBatchResult
from llm.router import GatewayExhausted
from scrapers._base import CandidateResult, SearchScrapeResult

GOLDEN = Path(__file__).with_name("golden")
GEM_URL = "https://mkp.gem.gov.in/computer-mouse/demo-mouse/p-300-400-cat.html"
DEMO_URL = "https://mkp.gem.gov.in/demo/p-123.html"


def _gem_product(url=GEM_URL):
    return {
        "url": url, "category_slug": "computer-mouse", "product_slug": "demo-mouse",
        "title": "Demo Wireless Mouse", "brand": "Demo", "model": None, "specs": {},
        "gem_price": 1000.0, "gem_price_type": "fixed", "currency": "INR",
    }


def _normalized(_gem=None):
    return {
        "canonical_name": "Demo Mouse", "brand": "Demo", "model": None,
        "key_identifiers": ["Demo"], "search_queries": ["demo wireless mouse"], "gem_price": 1000.0,
    }


def _cand(domain, url, price, title="Demo Mouse"):
    return CandidateResult(
        source_domain=domain, url=url, scraper_used=domain.split(".")[0],
        structured_fields={"title": title, "price": price, "currency": "INR"}, raw_page_text="",
    )


def _search(domain, candidates):
    return lambda query: SearchScrapeResult(source_domain=domain, query=query, candidates=list(candidates))


def _blocked(domain):
    return lambda query: SearchScrapeResult(source_domain=domain, query=query, candidates=[], blocked=True)


def _counting_search(domain, first, later):
    calls = {"n": 0}

    def search(query):
        calls["n"] += 1
        candidates = first if calls["n"] == 1 else later
        return SearchScrapeResult(source_domain=domain, query=query, candidates=list(candidates))

    return search


def _no_match(_normalized_product, _candidates):
    return MatchBatchResult()


def _no_expansion(*_args):
    return {"new_queries": [], "suggested_domains": [], "notes": ""}


def _llm_down(*_args, **_kwargs):
    raise GatewayExhausted("all LLM endpoints are used up")


def _base_kwargs():
    return dict(
        extract_fn=_gem_product,
        normalize_fn=_normalized,
        flipkart_search_fn=_search("flipkart.com", [_cand("flipkart.com", "https://www.flipkart.com/a", 900.0)]),
        amazon_search_fn=_search("amazon.in", [_cand("amazon.in", "https://www.amazon.in/b", 950.0)]),
        discover_fn=lambda queries, domains: [],
        generic_scrape_fn=lambda url: None,
        match_fn=_no_match,
        expand_search_fn=_no_expansion,
    )


def _clean(value):
    if isinstance(value, dict):
        return {
            key: _clean(item)
            for key, item in value.items()
            if key not in {"run_id", "trace_id", "checked_at", "seconds", "t", "elapsed", "duration"}
        }
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _run(**overrides):
    seen = []
    kwargs = _base_kwargs()
    kwargs.update(overrides)
    original = pipeline._similar_products
    pipeline._similar_products = lambda *_args: []
    try:
        result = pipeline.run_pipeline(GEM_URL, refresh=True, on_event=seen.append, **kwargs)
    finally:
        pipeline._similar_products = original
    return _clean(result), [event["type"] for event in seen]


def _scenario_all_blocked():
    return _run(flipkart_search_fn=_blocked("flipkart.com"), amazon_search_fn=_blocked("amazon.in"))


def _scenario_llm_unavailable():
    return _run(normalize_fn=_llm_down, match_fn=_llm_down, expand_search_fn=_llm_down)


def _scenario_nothing_found_with_retry():
    retry_queries = {"new_queries": ["demo wireless mouse cheap"], "suggested_domains": [], "notes": "try a shorter query"}
    return _run(
        flipkart_search_fn=_counting_search(
            "flipkart.com",
            [_cand("flipkart.com", "https://www.flipkart.com/a", 900.0)],
            [_cand("flipkart.com", "https://www.flipkart.com/a2", 880.0)],
        ),
        amazon_search_fn=_counting_search(
            "amazon.in",
            [_cand("amazon.in", "https://www.amazon.in/b", 950.0)],
            [_cand("amazon.in", "https://www.amazon.in/b2", 930.0)],
        ),
        expand_search_fn=lambda *_args: retry_queries,
    )


SCENARIOS = {
    "all-blocked": _scenario_all_blocked,
    "llm-unavailable": _scenario_llm_unavailable,
    "nothing-found-with-retry": _scenario_nothing_found_with_retry,
}


def _golden(name):
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


def test_demo_events_and_result_match_the_baseline(monkeypatch):
    # Sequential lanes: parallel shops finish in thread order, which differs
    # between platforms (the golden failed on ubuntu only). Same events, one
    # fixed order.
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", False)
    monkeypatch.setattr(demo, "_pause", lambda *_args, **_kwargs: None)
    seen = []
    result = demo.demo_runner(DEMO_URL, False, seen.append)

    expected_types = (GOLDEN / "demo-event-types.txt").read_text(encoding="utf-8").splitlines()
    assert [event["type"] for event in seen] == expected_types
    assert _clean(result) == _clean(json.loads((GOLDEN / "demo-result.json").read_text(encoding="utf-8")))


def test_all_shops_blocked_keeps_its_events_and_result(monkeypatch):
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", False)
    result, events = _scenario_all_blocked()
    expected = _golden("all-blocked")
    assert events == expected["events"]
    assert result == expected["result"]


def test_llm_unavailable_keeps_its_events_and_result(monkeypatch):
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", False)
    result, events = _scenario_llm_unavailable()
    expected = _golden("llm-unavailable")
    assert events == expected["events"]
    assert result == expected["result"]


def test_nothing_found_with_retry_keeps_its_events_and_result(monkeypatch):
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", False)
    result, events = _scenario_nothing_found_with_retry()
    expected = _golden("nothing-found-with-retry")
    assert events == expected["events"]
    assert result == expected["result"]


if __name__ == "__main__":
    import tempfile

    from core import config

    with tempfile.TemporaryDirectory() as tmp:
        pipeline.CACHE_DIR = Path(tmp) / "cache"
        pipeline.TRACES_DIR = Path(tmp) / "traces"
        config.CACHE_DIR = Path(tmp) / "cache"
        pipeline.CACHE_DIR.mkdir()
        pipeline.TRACES_DIR.mkdir()
        pipeline.PARALLEL_SEARCH = False
        demo._pause = lambda *_args, **_kwargs: None
        GOLDEN.mkdir(exist_ok=True)
        for name, scenario in SCENARIOS.items():
            result, events = scenario()
            payload = {"events": events, "result": result}
            (GOLDEN / f"{name}.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(name, len(events), "events")

        # The demo golden is regenerated the same way, with the demo pauses off.
        demo._pause = lambda *_args, **_kwargs: None
        seen = []
        demo_result = demo.demo_runner(DEMO_URL, False, seen.append)
        (GOLDEN / "demo-event-types.txt").write_text(
            "\n".join(event["type"] for event in seen) + "\n", encoding="utf-8"
        )
        (GOLDEN / "demo-result.json").write_text(
            json.dumps(_clean(demo_result), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print("demo", len(seen), "events")
