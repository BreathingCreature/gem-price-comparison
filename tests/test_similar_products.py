"""Stage 6.5: similar products — one LLM call, only when nothing matched.

The call is a nicety on top of a failed run: it must never replace a match,
never feed the cheapest price, and never be able to crash the run.
"""
import json

from core import pipeline
from llm import client
from llm.match import MatchBatchResult
from scrapers._base import CandidateResult, SearchScrapeResult

GEM_URL = "https://mkp.gem.gov.in/computer-mouse/demo-mouse/p-300-400-cat.html"


def _gem_product(url):
    return {"url": url, "category_slug": "computer-mouse", "product_slug": "demo-mouse",
            "title": "Demo Wireless Mouse", "brand": "Demo", "model": None, "specs": {},
            "gem_price": 1000.0, "gem_price_type": "fixed", "currency": "INR"}


def _normalized(_gem):
    return {"canonical_name": "Demo Wireless Mouse", "brand": "Demo", "model": None,
            "key_identifiers": ["Demo"], "search_queries": ["demo wireless mouse"], "gem_price": 1000.0}


def _cand(url, title, price=900.0, domain="flipkart.com"):
    return CandidateResult(source_domain=domain, url=url, scraper_used="x",
                           structured_fields={"title": title, "price": price, "currency": "INR"},
                           raw_page_text="")


def _nothing_matches(_n, candidates):
    return MatchBatchResult(all_decisions=[], confirmed_matches=[])


def _one_match(_n, candidates):
    d = {"candidate_url": candidates[0].url, "source_domain": candidates[0].source_domain,
         "is_match": True, "confidence": 0.9, "reason": "same", "price_used": 900.0,
         "extraction_source": "scraper"}
    return MatchBatchResult(all_decisions=[d], confirmed_matches=[d])


def _one_possible(_n, candidates):
    d = {"candidate_url": candidates[0].url, "source_domain": candidates[0].source_domain,
         "is_match": False, "confidence": 0.6, "reason": "maybe", "price_used": 900.0,
         "extraction_source": "scraper", "outcome": "possible", "why": "confidence 0.60 below 0.75"}
    return MatchBatchResult(all_decisions=[d], confirmed_matches=[], possible_matches=[d])


def _run(match_fn):
    def search(domain, url):
        return lambda q: SearchScrapeResult(
            source_domain=domain, query=q, candidates=[_cand(url, "Demo Wireless Mouse Silent")]
        )

    return pipeline.run_pipeline(
        GEM_URL,
        extract_fn=_gem_product,
        normalize_fn=_normalized,
        flipkart_search_fn=search("flipkart.com", "https://www.flipkart.com/a"),
        amazon_search_fn=lambda q: SearchScrapeResult(source_domain="amazon.in", query=q, candidates=[]),
        discover_fn=lambda q, d: [],
        generic_scrape_fn=lambda u: None,
        match_fn=match_fn,
        expand_search_fn=lambda *a: {"new_queries": [], "suggested_domains": [], "notes": ""},
    )


# --- the LLM call itself ------------------------------------------------------------

def test_find_similar_maps_indices_and_sorts_cheapest_first(monkeypatch):
    captured = {}

    def fake_call(system, user_content, *, max_tokens=0, client=None, purpose=""):
        captured["purpose"] = purpose
        captured["payload"] = json.loads(user_content)
        return {"similar": [{"index": 3, "why": "same type, bigger"}, {"index": 1, "why": "same type"}]}

    monkeypatch.setattr(client, "_call_llm", fake_call)
    listings = [
        {"url": "u1", "source_domain": "a.example", "structured_fields": {"title": "Alpha", "price": "₹1,500"}},
        {"url": "u2", "source_domain": "b.example", "structured_fields": {"title": "Beta", "price": "₹700"}},
        {"url": "u3", "source_domain": "c.example", "structured_fields": {"title": "Gamma", "price": "₹2,000"}},
    ]
    out = client.find_similar({"canonical_name": "Demo"}, listings)

    assert captured["purpose"] == "similar"
    assert captured["payload"]["listings"][0] == {
        "index": 1, "title": "Alpha", "price": "₹1,500", "shop": "a.example"
    }
    assert [row["url"] for row in out] == ["u1", "u3"]
    assert out[0]["price"] == "₹1,500"
    assert out[1]["why"] == "same type, bigger"


def test_find_similar_drops_bad_indices_and_caps_at_five(monkeypatch):
    def fake_call(*_a, **_k):
        return {"similar": [
            {"index": 99, "why": "no such listing"},
            {"index": 0, "why": "indices are 1-based"},
            {"index": "nope", "why": "not a number"},
            {"index": 2, "why": "a"}, {"index": 2, "why": "duplicate"},
            {"index": 3, "why": "b"}, {"index": 4, "why": "c"}, {"index": 5, "why": "d"},
            {"index": 6, "why": "e"}, {"index": 1, "why": "f"},
        ]}

    monkeypatch.setattr(client, "_call_llm", fake_call)
    listings = [
        {"url": f"u{i}", "source_domain": "x.example", "structured_fields": {"title": f"T{i}", "price": 100 * i}}
        for i in range(1, 7)
    ]
    out = client.find_similar({"canonical_name": "Demo"}, listings)
    assert len(out) == 5
    assert {row["url"] for row in out} <= {f"u{i}" for i in range(1, 7)}
    assert [row["price"] for row in out] == sorted(row["price"] for row in out)


def test_find_similar_without_listings_never_calls_the_llm(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("no listings means no question")

    monkeypatch.setattr(client, "_call_llm", boom)
    assert client.find_similar({"canonical_name": "Demo"}, []) == []


# --- the pipeline helper -----------------------------------------------------------

def _product_for_helper():
    return {"canonical_name": "Demo Wireless Mouse", "brand": "Demo", "model": "M650", "gem_price": 1000.0}


def test_the_helper_skips_model_hits_and_keeps_at_most_five(monkeypatch):
    captured = []

    def fake(normalized, listings):
        captured.extend(listings)
        return [{"shop": "x", "title": "t", "price": "₹1", "url": "u", "why": "w"}]

    monkeypatch.setattr(pipeline.llm_client, "find_similar", fake)
    candidates = [_cand("https://x/m650", "Demo Wireless Mouse M650")] + [
        _cand(f"https://x/{i}", f"Demo wireless mouse silent {i}") for i in range(8)
    ]
    out = pipeline._similar_products(_product_for_helper(), candidates, warnings=[])

    assert out == [{"shop": "x", "title": "t", "price": "₹1", "url": "u", "why": "w"}]
    assert len(captured) == 5
    assert all("M650" not in (row.get("structured_fields") or {}).get("title", "") for row in captured)


def test_the_helper_reports_a_failure_as_a_warning_only(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(pipeline.llm_client, "find_similar", boom)
    warnings = []
    out = pipeline._similar_products(
        _product_for_helper(), [_cand("https://x/a", "Demo wireless mouse silent")], warnings
    )
    assert out == []
    assert any("Similar products check failed" in w for w in warnings)


# --- the run ----------------------------------------------------------------------

def test_the_run_asks_for_similar_products_when_nothing_matched(monkeypatch):
    monkeypatch.setattr(pipeline, "RETRY_ENABLED", False)
    monkeypatch.setattr(pipeline, "DISCOVERY_ENABLED", False)
    calls = []

    def fake(normalized, listings):
        calls.append(listings)
        return [{"shop": "flipkart.com", "title": "Demo Mouse Blue", "price": "₹499",
                 "url": "https://www.flipkart.com/x", "why": "same kind of mouse"}]

    monkeypatch.setattr(pipeline.llm_client, "find_similar", fake)
    result = _run(_nothing_matches)

    assert len(calls) == 1
    assert result["matches"] == []
    assert result["similar"] == [{"shop": "flipkart.com", "title": "Demo Mouse Blue", "price": "₹499",
                                  "url": "https://www.flipkart.com/x", "why": "same kind of mouse"}]


def test_a_confirmed_match_means_no_similar_products_call(monkeypatch):
    monkeypatch.setattr(pipeline, "RETRY_ENABLED", False)
    monkeypatch.setattr(pipeline, "DISCOVERY_ENABLED", False)
    calls = []
    monkeypatch.setattr(pipeline.llm_client, "find_similar", lambda n, l: calls.append(l) or [])

    result = _run(_one_match)
    assert calls == []
    assert result["similar"] == []


def test_a_possible_match_also_suppresses_the_similar_products_call(monkeypatch):
    monkeypatch.setattr(pipeline, "RETRY_ENABLED", False)
    monkeypatch.setattr(pipeline, "DISCOVERY_ENABLED", False)
    calls = []
    monkeypatch.setattr(pipeline.llm_client, "find_similar", lambda n, l: calls.append(l) or [])

    result = _run(_one_possible)
    assert calls == []
    assert result["possible_matches"] and result["similar"] == []
