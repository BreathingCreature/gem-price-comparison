"""The on/off switches: parallel search, retry, and the gates."""
import json
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


def _cand(domain, url, price, title="Demo Mouse"):
    return CandidateResult(source_domain=domain, url=url, scraper_used=domain.split(".")[0],
                           structured_fields={"title": title, "price": price, "currency": "INR"}, raw_page_text="")


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


def _all_matches(_n, candidates):
    decisions = []
    for c in candidates:
        d = c.to_dict() if hasattr(c, "to_dict") else c
        decisions.append(
            {"candidate_url": d.get("url"), "source_domain": d.get("source_domain"), "is_match": True,
             "confidence": 0.9, "reason": "same", "price_used": (d.get("structured_fields") or {}).get("price"),
             "extraction_source": "scraper"}
        )
    return MatchBatchResult(all_decisions=decisions, confirmed_matches=list(decisions))


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
    assert config._flag("SOME_FLAG", True) is True  # empty â†’ default True
    assert config._flag("SOME_FLAG", False) is False  # empty â†’ default False


def test_flag_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_FLAG", raising=False)
    assert config._flag("SOME_FLAG", True) is True and config._flag("SOME_FLAG", False) is False


def test_normal_behaviour_is_the_default():
    # these tests assume the developer has not exported the switches
    assert config.RETRY_ENABLED is True and config.GATES_ENABLED is True
    assert config.PARALLEL_SEARCH is False and config.LLM_CACHE_ENABLED is False
    # Stage 6 defaults: the filter and the pre-gate are ON, verdicts are
    # reused, and the verifier runs four listings at a time.
    assert config.LLM_WORKERS == 4 and config.PREFILTER_TOP == 10
    assert config.PRICE_PREGATE is True and config.REUSE_VERDICTS is True
    assert config.STRICT_MODE is False
    assert config.DISCOVERY_ENABLED is True
    # checkpoint 18: light Flipkart browser + bounded discovery stage
    assert config.FLIPKART_LIGHT is True and config.DISCOVERY_TIME_LIMIT_SECONDS == 25.0


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
    # Real matching checks every candidate on its own, so the fake does the
    # same in both modes â€” lanes must not change the answer.
    sequential, _ = _run(match_fn=_all_matches)
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", True)
    parallel, _ = _run(match_fn=_all_matches)
    assert parallel["matches"] == sequential["matches"]
    assert parallel["not_found_on"] == sequential["not_found_on"]


# --- lanes: three searches at once, each checked the moment it lands -----------

def test_three_lanes_run_at_the_same_time(monkeypatch):
    # Discovery used to wait for the Flipkart/Amazon pair to finish first.
    # With GEM_PARALLEL=1 all three are lanes and must overlap.
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", True)
    gate = threading.Barrier(3, timeout=5)
    result, _ = _run(
        flipkart_search_fn=_search("flipkart.com", [_cand("flipkart.com", "https://www.flipkart.com/a", 900.0)], hook=gate.wait),
        amazon_search_fn=_search("amazon.in", [_cand("amazon.in", "https://www.amazon.in/dp/B0ABCDEFGH", 950.0)], hook=gate.wait),
        discover_fn=lambda q, d: (gate.wait(), [])[1],
        match_fn=_all_matches,
    )
    assert result["matches"]


def test_each_lane_checks_its_listings_right_away(monkeypatch):
    # Amazon answers while Flipkart is still searching â€” the Amazon lane
    # must verify its own listings during that wait, not after every search.
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", True)
    amazon_checked = threading.Event()

    def match_fn(normalized, candidates):
        if any(getattr(c, "source_domain", "") == "amazon.in" for c in candidates):
            amazon_checked.set()
        return _all_matches(normalized, candidates)

    def flipkart_search(q):
        assert amazon_checked.wait(timeout=5), "Amazon listings were still unchecked while Flipkart was searching"
        return SearchScrapeResult(source_domain="flipkart.com", query=q,
                                  candidates=[_cand("flipkart.com", "https://www.flipkart.com/a", 900.0)])

    result, _ = _run(flipkart_search_fn=flipkart_search, match_fn=match_fn)
    assert result["matches"]


def test_one_listing_found_by_two_lanes_is_counted_once(monkeypatch):
    # The same Flipkart URL can arrive from the direct search AND discovery â€”
    # the merge must keep one row: the first lane's copy.
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", True)
    url = "https://www.flipkart.com/m650/p/itm42"

    def discover_fn(queries, allowed_domains):
        return [{"query": queries[0], "raw_urls": [url], "filtered_urls": [url], "blocked": False, "error": None}]

    def generic_scrape_fn(u):
        return {"source_domain": "flipkart.com", "url": u, "scraper_used": "generic",
                "structured_fields": {"title": "Demo Mouse", "price": 950.0, "currency": "INR"}, "raw_page_text": ""}

    result, _ = _run(
        flipkart_search_fn=_search("flipkart.com", [_cand("flipkart.com", url, 900.0)]),
        amazon_search_fn=_search("amazon.in", []),
        discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn,
        match_fn=_all_matches,
    )
    assert len(result["matches"]) == 1
    assert result["matches"][0]["price_used"] == 900.0


def test_parallel_matches_are_sorted_by_price_across_lanes(monkeypatch):
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", True)
    result, _ = _run(
        flipkart_search_fn=_search("flipkart.com", [_cand("flipkart.com", "https://www.flipkart.com/a", 950.0)]),
        amazon_search_fn=_search("amazon.in", [_cand("amazon.in", "https://www.amazon.in/dp/B0ABCDEFGH", 900.0)]),
        match_fn=_all_matches,
    )
    assert [m["price_used"] for m in result["matches"]] == [900.0, 950.0]


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


def test_price_pre_gate_skips_the_llm_for_a_price_far_outside_the_band(monkeypatch):
    monkeypatch.setattr(matcher, "PRICE_PREGATE", True)
    calls = []
    monkeypatch.setattr(matcher, "verify_or_extract", lambda *a, **k: calls.append(1))
    out = matcher.match_candidates({"gem_price": 1000.0}, [_candidate_dict()])  # 39 vs 1000 â€” far below 0.15x
    assert calls == []  # the LLM was never asked
    assert out.confirmed_matches == []
    assert len(out.all_decisions) == 1 and out.all_decisions[0]["is_match"] is False


def test_price_pre_gate_is_on_by_default(monkeypatch):
    calls = []

    def fake(normalized, candidate, client=None):
        calls.append(candidate["url"])
        return {"candidate_url": candidate["url"], "source_domain": "shop.example", "is_match": True,
                "confidence": 0.95, "reason": "same", "price_used": 1000.0, "extraction_source": "scraper"}

    monkeypatch.setattr(matcher, "verify_or_extract", fake)
    # the fixture's price is 39 against a 1000 GeM price -> outside the band
    out = matcher.match_candidates({"gem_price": 1000.0}, [_candidate_dict()])
    assert calls == []  # Stage 6.4: never spends an LLM call on it
    assert out.confirmed_matches == []


def test_price_pre_gate_off_sends_the_listing_to_the_llm(monkeypatch):
    calls = []

    def fake(normalized, candidate, client=None):
        calls.append(candidate["url"])
        return {"candidate_url": candidate["url"], "source_domain": "shop.example", "is_match": True,
                "confidence": 0.95, "reason": "same", "price_used": 1000.0, "extraction_source": "scraper"}

    monkeypatch.setattr(matcher, "verify_or_extract", fake)
    monkeypatch.setattr(matcher, "PRICE_PREGATE", False)
    out = matcher.match_candidates({"gem_price": 1000.0}, [_candidate_dict()])
    assert calls == ["https://shop.example/p/1"]
    assert out.confirmed_matches == []  # the hard gate still rejects 39


def test_price_pre_gate_and_the_hard_gate_agree(monkeypatch):
    monkeypatch.setattr(matcher, "verify_or_extract", _fake_verify(39.0))
    without = matcher.match_candidates({"gem_price": 1000.0}, [_candidate_dict()])
    monkeypatch.setattr(matcher, "PRICE_PREGATE", True)
    with_pregate = matcher.match_candidates({"gem_price": 1000.0}, [_candidate_dict()])
    assert with_pregate.confirmed_matches == without.confirmed_matches == []


def test_price_pre_gate_respects_the_gates_switch(monkeypatch):
    # The pre-gate is the hard gate's fast path â€” gates off means both off.
    monkeypatch.setattr(matcher, "PRICE_PREGATE", True)
    monkeypatch.setattr(matcher, "GATES_ENABLED", False)
    monkeypatch.setattr(matcher, "verify_or_extract", _fake_verify(39.0))
    out = matcher.match_candidates({"gem_price": 1000.0}, [_candidate_dict()])
    assert len(out.confirmed_matches) == 1


# --- retry verdict reuse -----------------------------------------------------------------

def _retry_pair():
    """A run where Flipkart finds listing A first, listing B on the retry, and
    Amazon/discovery find nothing. Returns (match_fn, match_calls, result,
    first_url, second_url)."""
    seen = {"searches": 0}
    match_calls = []
    first_url, second_url = "https://www.flipkart.com/a", "https://www.flipkart.com/b"

    def flipkart_search(q):
        seen["searches"] += 1
        url = first_url if seen["searches"] == 1 else second_url
        return SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[_cand("flipkart.com", url, 900.0)])

    def match_fn(normalized, candidates):
        urls = [c.url for c in candidates]
        match_calls.append(urls)
        if len(match_calls) == 1:
            decisions = [{"candidate_url": u, "source_domain": "flipkart.com", "is_match": False,
                          "confidence": 0.2, "reason": "no", "price_used": None, "extraction_source": "scraper"} for u in urls]
            return MatchBatchResult(all_decisions=decisions, confirmed_matches=[])
        return _all_matches(normalized, candidates)

    result, _ = _run(flipkart_search_fn=flipkart_search, amazon_search_fn=_search("amazon.in", []), match_fn=match_fn)
    return match_fn, match_calls, result, first_url, second_url


def test_retry_reuses_first_pass_verdicts_when_switched_on(monkeypatch):
    monkeypatch.setattr(pipeline, "REUSE_VERDICTS", True)
    _, match_calls, result, first_url, second_url = _retry_pair()
    assert match_calls[0] == [first_url]
    assert match_calls[1] == [second_url]  # only the new listing is asked about
    assert [m["candidate_url"] for m in result["matches"]] == [second_url]


def test_retry_without_reuse_asks_about_all_listings_again(monkeypatch):
    monkeypatch.setattr(pipeline, "REUSE_VERDICTS", False)
    _, match_calls, _, first_url, second_url = _retry_pair()
    assert match_calls[1] == [first_url, second_url]


def test_retry_reuse_asks_again_about_skipped_listings(monkeypatch):
    monkeypatch.setattr(pipeline, "REUSE_VERDICTS", True)
    seen = {"searches": 0}
    match_calls = []
    first_url, second_url = "https://www.flipkart.com/a", "https://www.flipkart.com/b"

    def flipkart_search(q):
        seen["searches"] += 1
        url = first_url if seen["searches"] == 1 else second_url
        return SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[_cand("flipkart.com", url, 900.0)])

    def match_fn(normalized, candidates):
        match_calls.append([c.url for c in candidates])
        if len(match_calls) == 1:
            return MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=[first_url])
        return _all_matches(normalized, candidates)

    result, _ = _run(flipkart_search_fn=flipkart_search, amazon_search_fn=_search("amazon.in", []), match_fn=match_fn)
    # a skipped listing never got a verdict, so the retry must ask again
    assert match_calls[1] == [first_url, second_url]
    assert result["matches"]


# --- discovery switch --------------------------------------------------------------------

def test_discovery_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(pipeline, "DISCOVERY_ENABLED", False)
    calls = []

    def discover_fn(queries, allowed_domains):
        calls.append(1)
        return []

    result, seen = _run(discover_fn=discover_fn)
    assert calls == []
    names = [e["name"] for e in seen if e["type"] == "stage"]
    assert "discovery" not in names
    assert result["matches"]  # the direct shops still work


def test_discovery_switch_applies_to_the_lanes_too(monkeypatch):
    monkeypatch.setattr(pipeline, "DISCOVERY_ENABLED", False)
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", True)
    calls = []
    result, _ = _run(discover_fn=lambda q, d: (calls.append(1), [])[1], match_fn=_all_matches)
    assert calls == []
    assert result["matches"]


def test_discovery_stays_off_on_the_retry(monkeypatch):
    monkeypatch.setattr(pipeline, "DISCOVERY_ENABLED", False)
    calls = []

    def discover_fn(queries, allowed_domains):
        calls.append(1)
        return []

    result, _ = _run(discover_fn=discover_fn, match_fn=_nothing_matches)
    assert calls == []
    assert result["matches"] == []


# --- prefilter switch --------------------------------------------------------------------

def _two_flipkart_listings(q):
    return SearchScrapeResult(source_domain="flipkart.com", query=q, candidates=[
        _cand("flipkart.com", "https://www.flipkart.com/good/p/itm1", 900.0, title="Demo Wireless Mouse"),
        _cand("flipkart.com", "https://www.flipkart.com/bad/p/itm2", 880.0, title="Desk Mat Pad XL Large"),
    ])


def test_prefilter_drops_the_worst_listings_per_shop_and_records_them(monkeypatch):
    monkeypatch.setattr(pipeline, "PREFILTER_TOP", 1)
    checked = []

    def match_fn(normalized, candidates):
        checked.extend(c.url for c in candidates)
        return _all_matches(normalized, candidates)

    result, _ = _run(flipkart_search_fn=_two_flipkart_listings, match_fn=match_fn)
    assert "https://www.flipkart.com/bad/p/itm2" not in checked  # never sent to the LLM
    assert result["matches"]
    trace = json.loads((pipeline.TRACES_DIR / f"{result['trace_id']}.json").read_text(encoding="utf-8"))
    dropped = trace["stages"]["prefilter"]["dropped"]
    assert [d["url"] for d in dropped] == ["https://www.flipkart.com/bad/p/itm2"]


def test_prefilter_applies_inside_the_lanes_too(monkeypatch):
    monkeypatch.setattr(pipeline, "PREFILTER_TOP", 1)
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", True)
    checked = []

    def match_fn(normalized, candidates):
        checked.extend(c.url for c in candidates)
        return _all_matches(normalized, candidates)

    result, _ = _run(flipkart_search_fn=_two_flipkart_listings, match_fn=match_fn)
    assert "https://www.flipkart.com/bad/p/itm2" not in checked
    assert result["matches"]
    trace = json.loads((pipeline.TRACES_DIR / f"{result['trace_id']}.json").read_text(encoding="utf-8"))
    dropped = trace["stages"]["prefilter"]["dropped"]
    assert [d["url"] for d in dropped] == ["https://www.flipkart.com/bad/p/itm2"]


# --- lane isolation: one crashing lane must not kill the run --------------------

def _crash(*args, **kwargs):
    raise RuntimeError("chrome died")


def test_a_crashing_lane_does_not_kill_the_other_lanes(monkeypatch):
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", True)
    result, seen = _run(
        flipkart_search_fn=_crash,
        amazon_search_fn=_search("amazon.in", [_cand("amazon.in", "https://www.amazon.in/dp/B0ABCDEFGH", 950.0)]),
        match_fn=_all_matches,
    )
    assert [m["candidate_url"] for m in result["matches"]] == ["https://www.amazon.in/dp/B0ABCDEFGH"]
    assert any("flipkart: RuntimeError: chrome died" in issue for issue in result["search_issues"])
    assert [e["lane"] for e in seen if e["type"] == "lane_failed"] == ["flipkart"]


def test_when_every_lane_crashes_the_error_is_raised(monkeypatch):
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", True)

    def boom_discovery(queries, domains):
        raise RuntimeError("discovery down")

    with pytest.raises(RuntimeError):
        _run(flipkart_search_fn=_crash, amazon_search_fn=_crash, discover_fn=boom_discovery)


def test_sequential_mode_still_raises_a_search_crash():
    with pytest.raises(RuntimeError, match="chrome died"):
        _run(flipkart_search_fn=_crash)


# --- strict mode and the shared verdict cache -----------------------------------

def test_strict_mode_runs_the_lanes_but_sends_every_listing(monkeypatch):
    # GEM_STRICT=1: the old verifier path (every listing, one at a time), but
    # the searches still run together.
    monkeypatch.setattr(pipeline, "STRICT_MODE", True)
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", False)
    checked = []

    def match_fn(normalized, candidates):
        checked.extend(c.url for c in candidates)
        return _all_matches(normalized, candidates)

    result, seen = _run(flipkart_search_fn=_two_flipkart_listings, match_fn=match_fn)
    names = [e["name"] for e in seen if e["type"] == "stage"]
    assert "search_both" in names and "search_flipkart" not in names
    assert "https://www.flipkart.com/bad/p/itm2" in checked  # no pre-filter
    trace = json.loads((pipeline.TRACES_DIR / f"{result['trace_id']}.json").read_text(encoding="utf-8"))
    assert "prefilter" not in trace["stages"]


def test_strict_mode_checks_one_listing_at_a_time(monkeypatch):
    from core import parallel

    monkeypatch.setattr(matcher, "STRICT_MODE", True)
    seen = {}
    real = parallel.run_many

    def spy(jobs, workers=1):
        seen["workers"] = workers
        return real(jobs, workers=workers)

    monkeypatch.setattr(matcher, "run_many", spy)

    def fake(normalized, candidate, client=None):
        return {"candidate_url": candidate["url"], "source_domain": candidate["source_domain"],
                "is_match": True, "confidence": 0.9, "reason": "same", "price_used": 900.0,
                "extraction_source": "scraper"}

    monkeypatch.setattr(matcher, "verify_or_extract", fake)
    candidates = [{"url": f"https://x.com/{i}", "source_domain": "x.com",
                   "structured_fields": {"title": f"t{i}"}, "raw_page_text": ""} for i in range(3)]
    matcher.match_candidates({"canonical_name": "Demo"}, candidates)
    assert seen["workers"] == 1


def test_the_run_hands_the_real_matcher_one_shared_verdict_cache(monkeypatch):
    monkeypatch.setattr(pipeline, "PARALLEL_SEARCH", True)
    caches = []

    def fake_match(normalized, candidates, *, verdict_cache=None):
        caches.append(verdict_cache)
        return _all_matches(normalized, candidates)

    monkeypatch.setattr(matcher, "match_candidates", fake_match)
    result, _ = _run(
        flipkart_search_fn=_two_flipkart_listings,
        amazon_search_fn=_search("amazon.in", [_cand("amazon.in", "https://www.amazon.in/dp/B0ABCDEFGH", 950.0)]),
        match_fn=matcher.match_candidates,
    )
    assert result["matches"]
    assert len(caches) >= 2
    assert all(cache is caches[0] for cache in caches)
    assert caches[0] == {}


def test_a_possible_match_is_reported_and_not_counted_as_not_found(monkeypatch):
    def possible(_n, candidates):
        if not candidates:
            return MatchBatchResult()
        d = {"candidate_url": candidates[0].url, "source_domain": candidates[0].source_domain,
             "is_match": False, "confidence": 0.6, "reason": "maybe", "price_used": 900.0,
             "extraction_source": "scraper", "outcome": "possible", "why": "confidence 0.60 below 0.75"}
        return MatchBatchResult(all_decisions=[d], confirmed_matches=[], possible_matches=[d])

    result, _ = _run(
        flipkart_search_fn=_two_flipkart_listings,
        amazon_search_fn=_search("amazon.in", []),
        match_fn=possible,
    )
    assert result["matches"] == []
    assert [d["candidate_url"] for d in result["possible_matches"]] == ["https://www.flipkart.com/good/p/itm1"]
    assert "flipkart.com" not in result["not_found_on"]
    assert any("possibly the same product" in issue for issue in result["search_issues"])
