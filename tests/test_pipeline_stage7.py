"""Stage 7: shop cards, the shown-then-updated result, retry gating, result v2."""
import time

import pytest

from core import pipeline, result, retry
from core.result import build_result, shop_entry
from llm.match import MatchBatchResult
from scrapers._base import CandidateResult, SearchScrapeResult

GEM_URL = "https://mkp.gem.gov.in/computer-mouse/demo-mouse/p-300-400-cat.html"


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


def _search(domain, candidates, delay=0.0):
    def fn(query):
        if delay:
            time.sleep(delay)
        return SearchScrapeResult(source_domain=domain, query=query, candidates=list(candidates))
    return fn


def _all_matches(_normalized_product, candidates):
    decisions = [
        {"candidate_url": c.url, "source_domain": c.source_domain, "is_match": True, "confidence": 0.9,
         "reason": "same", "price_used": c.structured_fields["price"], "extraction_source": "scraper"}
        for c in candidates
    ]
    return MatchBatchResult(all_decisions=decisions, confirmed_matches=list(decisions))


def _nothing_matches(_normalized_product, candidates):
    return MatchBatchResult()


def _no_expansion(*_args):
    return {"new_queries": [], "suggested_domains": [], "notes": ""}


def _run(url=GEM_URL, **overrides):
    seen = []
    kwargs = dict(
        extract_fn=lambda u: _gem_product(u),
        normalize_fn=_normalized,
        flipkart_search_fn=_search("flipkart.com", [_cand("flipkart.com", "https://www.flipkart.com/a", 900.0)]),
        amazon_search_fn=_search("amazon.in", [_cand("amazon.in", "https://www.amazon.in/b", 950.0)]),
        discover_fn=lambda queries, domains: [],
        generic_scrape_fn=lambda url: None,
        match_fn=_all_matches,
        expand_search_fn=_no_expansion,
    )
    kwargs.update(overrides)
    result_dict = pipeline.run_pipeline(url, refresh=True, on_event=seen.append, **kwargs)
    return result_dict, seen


def _types(seen):
    return [e["type"] for e in seen]


def _entry(**kwargs):
    base = dict(status=result.SHOP_NOT_FOUND, reason="searched, nothing confirmed")
    base.update(kwargs)
    return shop_entry(**base)


def _candidate_dict(domain, url="https://shop.example/p/1", title="Demo Mouse"):
    return {"url": url, "source_domain": domain, "structured_fields": {"title": title, "price": 500.0},
            "raw_page_text": "x"}


# --- result.py ---------------------------------------------------------------

def test_a_found_shop_without_a_match_is_downgraded():
    built = build_result(
        gem_url=GEM_URL, gem_product=_gem_product(), normalized=_normalized(),
        shops={"shop.example": _entry(status=result.SHOP_FOUND, reason="")},
        matches=[], comparison={"gem_price": 1000.0},
    )
    assert built["shops"]["shop.example"]["status"] == result.SHOP_NOT_FOUND
    assert built["shops"]["shop.example"]["reason"]


def test_cheapest_needs_a_price_and_a_gem_price():
    row = {"candidate_url": "https://shop.example/p/1", "source_domain": "shop.example",
           "price_used": 500.0, "confidence": 0.9, "is_match": True}
    built = build_result(
        gem_url=GEM_URL, gem_product=_gem_product(), normalized=_normalized(),
        shops={"shop.example": _entry(status=result.SHOP_FOUND, reason="")},
        matches=[row], comparison={"gem_price": 1000.0},
    )
    assert built["cheapest"]["price"] == 500.0 and built["cheapest"]["saving_vs_gem"] == 500.0

    no_gem_price = dict(_gem_product(), gem_price=None)
    built = build_result(
        gem_url=GEM_URL, gem_product=no_gem_price, normalized=_normalized(),
        shops={"shop.example": _entry(status=result.SHOP_FOUND, reason="")},
        matches=[row], comparison={},
    )
    assert built["cheapest"] is None


def test_an_unfinished_shop_always_adds_a_warning():
    built = build_result(
        gem_url=GEM_URL, gem_product=_gem_product(), normalized=_normalized(),
        shops={"croma.com": _entry(status=result.SHOP_INCOMPLETE, reason="2 listings could not be checked")},
        matches=[], comparison={"gem_price": 1000.0},
    )
    assert any("croma.com: incomplete" in warning for warning in built["warnings"])


def test_matches_are_cheapest_first_and_out_of_stock_last():
    rows = [
        {"candidate_url": "https://a.example/1", "source_domain": "a.example", "price_used": 100.0,
         "in_stock": False, "confidence": 0.9},
        {"candidate_url": "https://b.example/2", "source_domain": "b.example", "price_used": 900.0,
         "in_stock": True, "confidence": 0.9},
        {"candidate_url": "https://c.example/3", "source_domain": "c.example", "price_used": 800.0,
         "in_stock": True, "confidence": 0.9},
    ]
    built = build_result(
        gem_url=GEM_URL, gem_product=_gem_product(), normalized=_normalized(),
        shops={name: _entry(status=result.SHOP_FOUND, reason="") for name in ("a.example", "b.example", "c.example")},
        matches=rows, comparison={"gem_price": 1000.0},
    )
    assert [row["price"] for row in built["matches"]] == [800.0, 900.0, 100.0]
    assert built["cheapest"]["shop"] == "c.example"


def test_v2_keys_and_legacy_keys_are_both_present():
    built = build_result(
        gem_url=GEM_URL, gem_product=_gem_product(), normalized=_normalized(),
        shops={}, matches=[], possible_matches=[], similar=[], warnings=[], search_issues=[],
        not_found_on=["flipkart.com"], skipped=[], comparison={"gem_price": 1000.0}, trace_id="abc123",
    )
    assert built["result_version"] == 2
    assert built["run_id"] == "abc123" and built["trace_id"] == "abc123"
    assert built["state"] == "done" and built["checked_at"]
    assert set(built["gem"]) >= {"url", "title", "canonical_name", "brand", "model", "price", "described_by"}
    for legacy in ("gem_product", "not_found_on", "search_issues", "skipped", "comparison"):
        assert legacy in built
    assert built["compared_with"] == []


# --- core/retry.py -----------------------------------------------------------

def test_only_retryable_shops_are_picked():
    shops = {
        "flipkart.com": _entry(status=result.SHOP_FOUND, reason=""),
        "amazon.in": _entry(status=result.SHOP_INCOMPLETE, reason="1 listing could not be checked",
                            retryable=True),
        "croma.com": _entry(status=result.SHOP_NOT_SEARCHED, reason="blocked/CAPTCHA", retryable=False),
        "other": _entry(status=result.SHOP_NOT_FOUND, reason="searched, nothing confirmed", retryable=True),
    }
    assert retry.retryable_shops(shops, nothing_found=False) == ["amazon.in"]
    assert retry.retryable_shops(shops, nothing_found=True) == ["amazon.in", "other"]


def test_a_retried_shop_is_not_picked_again():
    shops = {"amazon.in": _entry(status=result.SHOP_INCOMPLETE, reason="x", retried=True, retryable=True)}
    assert retry.retryable_shops(shops, nothing_found=True) == []


def test_fresh_queries_drops_repeats_and_blanks():
    expansion = {"new_queries": [" demo mouse 5g ", "demo mouse 5g", "demo wireless mouse", "", "m650"]}
    assert retry.fresh_queries(expansion, ["demo wireless mouse"]) == ["demo mouse 5g", "m650"]
    assert retry.fresh_queries({"new_queries": []}, []) == []


# --- shop cards --------------------------------------------------------------

def test_a_blocked_shop_is_not_searched_and_retryable():
    shops = pipeline._shop_entries(
        flipkart_results=[{"blocked": True, "error": None}], amazon_results=[], discovery_results=[],
        flipkart_candidates=[], amazon_candidates=[], generic_candidates=[],
        match_result=MatchBatchResult(), prefilter_dropped=[], lane_errors=[], lane_seconds={},
        nothing_found=True,
    )
    assert shops["flipkart.com"]["status"] == result.SHOP_NOT_SEARCHED
    assert shops["flipkart.com"]["reason"] == "blocked/CAPTCHA"
    assert shops["flipkart.com"]["retryable"] is True


def test_unchecked_listings_make_a_shop_incomplete_not_not_found():
    shops = pipeline._shop_entries(
        flipkart_results=[{"blocked": False, "error": None}], amazon_results=[], discovery_results=[],
        flipkart_candidates=[_candidate_dict("flipkart.com", "https://www.flipkart.com/x")],
        amazon_candidates=[], generic_candidates=[],
        match_result=MatchBatchResult(skipped=["https://www.flipkart.com/x"]),
        prefilter_dropped=[], lane_errors=[], lane_seconds={}, nothing_found=True,
    )
    entry = shops["flipkart.com"]
    assert entry["status"] == result.SHOP_INCOMPLETE
    assert "could not be checked" in entry["reason"]
    assert entry["matches"] == 0


def test_a_lane_over_its_limit_is_timed_out(monkeypatch):
    monkeypatch.setattr(pipeline, "LANE_TIME_LIMIT_SECONDS", 10.0)
    shops = pipeline._shop_entries(
        flipkart_results=[{"blocked": False, "error": None}], amazon_results=[], discovery_results=[],
        flipkart_candidates=[], amazon_candidates=[], generic_candidates=[],
        match_result=MatchBatchResult(), prefilter_dropped=[], lane_errors=[],
        lane_seconds={"flipkart": 42.0}, nothing_found=True,
    )
    entry = shops["flipkart.com"]
    assert entry["status"] == result.SHOP_TIMED_OUT
    assert "42" in entry["reason"] and entry["retryable"] is True


def test_discovery_off_marks_other_not_searched_and_unretryable():
    shops = pipeline._shop_entries(
        flipkart_results=[], amazon_results=[], discovery_results=[],
        flipkart_candidates=[], amazon_candidates=[], generic_candidates=[],
        match_result=MatchBatchResult(), prefilter_dropped=[], lane_errors=[], lane_seconds={},
        nothing_found=True, discovered=False,
    )
    assert shops["other"]["status"] == result.SHOP_NOT_SEARCHED
    assert shops["other"]["retryable"] is False


def test_a_discovery_crash_is_reported_on_the_other_card():
    shops = pipeline._shop_entries(
        flipkart_results=[], amazon_results=[], discovery_results=[],
        flipkart_candidates=[], amazon_candidates=[], generic_candidates=[],
        match_result=MatchBatchResult(), prefilter_dropped=[],
        lane_errors=["other_shops: RuntimeError: discovery down"], lane_seconds={}, nothing_found=True,
    )
    assert shops["other"]["status"] == result.SHOP_INCOMPLETE
    assert "discovery down" in shops["other"]["reason"]
    assert shops["other"]["retryable"] is False


# --- the shown-then-updated run ----------------------------------------------

def test_the_first_result_is_shown_then_the_retry_updates_it(monkeypatch):
    monkeypatch.setattr(pipeline, "LANE_TIME_LIMIT_SECONDS", 0.001)
    calls = {"flipkart": 0, "amazon": 0}

    def flipkart_fn(query):
        calls["flipkart"] += 1
        if calls["flipkart"] == 1:
            time.sleep(0.01)
            return SearchScrapeResult(source_domain="flipkart.com", query=query, candidates=[])
        return SearchScrapeResult(
            source_domain="flipkart.com", query=query,
            candidates=[_cand("flipkart.com", "https://www.flipkart.com/a", 900.0)],
        )

    def amazon_fn(query):
        calls["amazon"] += 1
        return SearchScrapeResult(
            source_domain="amazon.in", query=query,
            candidates=[_cand("amazon.in", "https://www.amazon.in/b", 950.0)],
        )

    result_dict, seen = _run(
        flipkart_search_fn=flipkart_fn, amazon_search_fn=amazon_fn,
        expand_search_fn=lambda *a: {"new_queries": ["demo mouse better"], "suggested_domains": [], "notes": ""},
    )

    headline = next(e for e in seen if e["type"] == "run_finished")
    assert headline["matches"] == 1  # Amazon only, before the retry

    types = _types(seen)
    assert types.index("run_finished") < types.index("retry_started")
    assert types.index("retry_started") < types.index("shop_updated")
    assert types.index("shop_updated") < types.index("run_updated") < types.index("done")
    assert types[-1] == "done"

    retry_event = next(e for e in seen if e["type"] == "retry_started")
    assert retry_event["shops"] == ["flipkart.com"]
    updated = [e for e in seen if e["type"] == "shop_updated"]
    assert [e["shop"] for e in updated] == ["flipkart.com"]

    # The found shop is not searched again; the timed-out one is, once.
    assert calls == {"flipkart": 2, "amazon": 1}
    assert result_dict["shops"]["flipkart.com"]["retried"] is True
    assert result_dict["shops"]["flipkart.com"]["retryable"] is False
    assert result_dict["shops"]["amazon.in"]["retried"] is False
    assert sorted(m["price"] for m in result_dict["matches"]) == [900.0, 950.0]


def test_a_found_run_does_not_retry(monkeypatch):
    monkeypatch.setattr(pipeline, "LANE_TIME_LIMIT_SECONDS", 150.0)
    calls = []

    def expansion(*_args):
        calls.append(1)
        return {"new_queries": ["demo mouse better"], "suggested_domains": [], "notes": ""}

    _, seen = _run(expand_search_fn=expansion)
    assert calls == []
    assert "retry_started" not in _types(seen)


def test_result_v2_is_returned_and_the_shops_are_in_the_trace():
    result_dict, _ = _run()
    assert result_dict["result_version"] == 2
    assert result_dict["run_id"] == result_dict["trace_id"]
    assert result_dict["state"] == "done"
    assert result_dict["shops"]["flipkart.com"]["status"] == result.SHOP_FOUND
    assert result_dict["shops"]["amazon.in"]["status"] == result.SHOP_FOUND
    assert result_dict["shops"]["flipkart.com"]["matches"] == 1

    import json
    trace = json.loads((pipeline.TRACES_DIR / f"{result_dict['trace_id']}.json").read_text(encoding="utf-8"))
    assert trace["stages"]["shops"]["flipkart.com"]["status"] == result.SHOP_FOUND
    assert trace["final_result"]["matches"]


def test_similar_ready_is_announced_when_nothing_matched(monkeypatch):
    rows = [{"shop": "croma.com", "title": "Demo Mouse Pad", "price": 299.0,
             "url": "https://www.croma.com/pad", "why": "same kind of accessory"}]
    monkeypatch.setattr(pipeline, "_similar_products", lambda normalized, candidates, warnings: list(rows))
    result_dict, seen = _run(match_fn=_nothing_matches)
    assert "similar_ready" in _types(seen)
    ready = next(e for e in seen if e["type"] == "similar_ready")
    assert ready["similar"][0]["title"] == "Demo Mouse Pad"
    assert result_dict["similar"][0]["price"] == 299.0
    assert result_dict["cheapest"] is None


def test_a_blocked_lane_is_retried_and_the_cheapest_changes():
    calls = {"flipkart": 0}

    def flipkart_fn(query):
        calls["flipkart"] += 1
        if calls["flipkart"] == 1:
            return SearchScrapeResult(source_domain="flipkart.com", query=query, candidates=[], blocked=True)
        return SearchScrapeResult(
            source_domain="flipkart.com", query=query,
            candidates=[_cand("flipkart.com", "https://www.flipkart.com/a", 900.0)],
        )

    result_dict, seen = _run(
        flipkart_search_fn=flipkart_fn,
        amazon_search_fn=_search("amazon.in", []),
        expand_search_fn=lambda *a: {"new_queries": ["demo mouse 5g"], "suggested_domains": [], "notes": ""},
    )

    types = _types(seen)
    assert types.index("retry_started") < types.index("shop_updated") < types.index("run_updated")
    updated = [e for e in seen if e["type"] == "shop_updated"]
    flipkart_update = next(e for e in updated if e["shop"] == "flipkart.com")
    assert flipkart_update["status"] == result.SHOP_FOUND
    assert calls["flipkart"] == 2
    assert result_dict["cheapest"]["shop"] == "flipkart.com"
    assert result_dict["cheapest"]["saving_vs_gem"] == 100.0
    assert result_dict["shops"]["flipkart.com"]["status"] == result.SHOP_FOUND


def test_a_used_up_llm_skips_the_retry_and_warns():
    from llm.router import GatewayExhausted

    calls = {"flipkart": 0}

    def flipkart_fn(query):
        calls["flipkart"] += 1
        return SearchScrapeResult(source_domain="flipkart.com", query=query, candidates=[])

    def exhausted(*_args):
        raise GatewayExhausted("LLM limits used up until 00:00 for: a/m")

    result_dict, seen = _run(
        flipkart_search_fn=flipkart_fn, amazon_search_fn=_search("amazon.in", []),
        match_fn=_nothing_matches, expand_search_fn=exhausted,
    )

    assert calls["flipkart"] == 1  # no retry search
    assert any("used up" in warning for warning in result_dict["warnings"])
    assert "shop_updated" not in _types(seen)
    assert result_dict["shops"]["flipkart.com"]["retried"] is False


def test_zero_everything_is_not_found_only_for_cleanly_searched_shops():
    result_dict, _ = _run(
        flipkart_search_fn=_search("flipkart.com", []),
        amazon_search_fn=_search("amazon.in", []),
        match_fn=_nothing_matches,
    )
    assert result_dict["shops"]["flipkart.com"]["status"] == result.SHOP_NOT_FOUND
    assert result_dict["shops"]["amazon.in"]["status"] == result.SHOP_NOT_FOUND
    assert result_dict["shops"]["other"]["status"] == result.SHOP_NOT_FOUND
    assert result_dict["shops"]["flipkart.com"]["reason"]
    assert result_dict["matches"] == [] and result_dict["cheapest"] is None
    assert "flipkart.com" in result_dict["not_found_on"]


def test_every_v2_field_has_the_right_type():
    result_dict, _ = _run()
    assert isinstance(result_dict["result_version"], int)
    assert isinstance(result_dict["run_id"], str) and isinstance(result_dict["trace_id"], str)
    assert isinstance(result_dict["checked_at"], str) and "T" in result_dict["checked_at"]
    assert isinstance(result_dict["from_cache"], bool)
    assert result_dict["state"] in ("shown", "updating", "done")
    assert isinstance(result_dict["gem"], dict) and isinstance(result_dict["gem"]["price"], dict)
    assert isinstance(result_dict["shops"], dict)
    for entry in result_dict["shops"].values():
        assert set(entry) >= {"status", "reason", "seen", "dropped", "checked", "matches",
                              "retried", "retryable", "seconds"}
        assert entry["status"] in result.SHOP_STATUSES
        assert isinstance(entry["seen"], int) and isinstance(entry["retryable"], bool)
    assert isinstance(result_dict["matches"], list) and isinstance(result_dict["possible_matches"], list)
    assert isinstance(result_dict["similar"], list)
    assert isinstance(result_dict["compared_with"], list)
    assert isinstance(result_dict["warnings"], list)
    assert isinstance(result_dict["cheapest"], dict)
    assert isinstance(result_dict["cheapest"]["saving_vs_gem"], (int, float))


def test_shop_started_is_announced_for_every_lane():
    _, seen = _run()
    started = [e["shop"] for e in seen if e["type"] == "shop_started"]
    assert started[0] == "flipkart.com"
    assert "amazon.in" in started and "other" in started
    finished = {e["shop"] for e in seen if e["type"] == "shop_finished"}
    assert {"flipkart.com", "amazon.in", "other"} <= finished


def test_lane_time_limit_refuses_a_zero(tmp_path, monkeypatch):
    import importlib
    import core.config as config_mod

    monkeypatch.setenv("GEM_LANE_TIME_LIMIT", "0")
    importlib.reload(config_mod)
    assert config_mod.LANE_TIME_LIMIT_SECONDS == 150.0

    monkeypatch.setenv("GEM_LANE_TIME_LIMIT", "30")
    importlib.reload(config_mod)
    assert config_mod.LANE_TIME_LIMIT_SECONDS == 30.0

    monkeypatch.delenv("GEM_LANE_TIME_LIMIT", raising=False)
    importlib.reload(config_mod)
