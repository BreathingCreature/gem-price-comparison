"""A direct lane runs one more normalized query before calling the shop empty.

search_direct stops as soon as a query returns ENOUGH relevant cards, so the
query that would have held the exact listing can stay unrun. The lane now
retries once with the next unused query when it confirmed nothing - never
after a confirmed match, never more than one extra search, and never twice
for the same URL.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import pipeline  # noqa: E402
from llm import match as matcher  # noqa: E402
from llm.match import MatchBatchResult  # noqa: E402
from scrapers._base import CandidateResult, SearchScrapeResult  # noqa: E402

NORMALIZED = {
    "canonical_name": "Acme Widget 100",
    "brand": "Acme",
    "model": "W-100",
    "key_identifiers": ["Acme", "W-100"],
    "gem_price": 1000.0,
}


def _cand(url):
    return CandidateResult(
        source_domain="amazon.in",
        url=url,
        scraper_used="amazon",
        structured_fields={"title": "Acme Widget W-100", "price": 900.0, "currency": "INR"},
        raw_page_text="",
    )


def _result(query, candidates):
    return SearchScrapeResult(source_domain="amazon.in", query=query, candidates=list(candidates))


def _matcher(seen):
    def match(_normalized, candidates):
        decisions = []
        for candidate in candidates:
            d = candidate.to_dict()
            seen.append(d["url"])
            hit = "want" in d["url"].lower()
            decisions.append(
                {
                    "candidate_url": d["url"],
                    "source_domain": d["source_domain"],
                    "is_match": hit,
                    "confidence": 0.95 if hit else 0.9,
                    "reason": "same model" if hit else "different model",
                    "price_used": 900.0,
                    "outcome": matcher.OUTCOME_MATCH if hit else matcher.OUTCOME_NO_MATCH,
                }
            )
        return MatchBatchResult(
            all_decisions=decisions,
            confirmed_matches=[d for d in decisions if d["outcome"] == matcher.OUTCOME_MATCH],
        )

    return match


def _empty_search(domain="flipkart.com"):
    def fn(query):
        return SearchScrapeResult(source_domain=domain, query=query, candidates=[])

    return fn


def _run(amazon_search_fn, queries, match_fn):
    lane_errors, lane_seconds = [], {}
    lanes = pipeline._run_first_pass_lanes(
        _empty_search(),
        amazon_search_fn,
        queries,
        discover_fn=lambda *a, **k: [],
        generic_scrape_fn=lambda *a, **k: None,
        normalized=NORMALIZED,
        match_fn=match_fn,
        lane_errors=lane_errors,
        lane_seconds=lane_seconds,
    )
    flipkart, amazon, other = lanes
    return amazon


def test_the_lane_retries_the_unused_query_and_keeps_its_match():
    calls, seen = [], []

    def amazon(query):
        calls.append(query)
        if query == "q1":
            # Five relevant cards, none of them the product: the early exit
            # stops here, exactly like the live P03 run.
            return _result(query, [_cand(f"https://www.amazon.in/dp/OTHER{i}") for i in range(5)])
        return _result(query, [_cand("https://www.amazon.in/dp/WANT1")])

    _results, candidates, lane_match, _dropped = _run(amazon, ["q1", "q2"], _matcher(seen))

    assert calls == ["q1", "q2"]
    assert [d["candidate_url"] for d in lane_match.confirmed_matches] == ["https://www.amazon.in/dp/WANT1"]
    assert len(candidates) == 6


def test_a_confirmed_first_query_does_not_spend_a_second_search():
    calls, seen = [], []

    def amazon(query):
        calls.append(query)
        cards = [_cand("https://www.amazon.in/dp/WANT1")] + [
            _cand(f"https://www.amazon.in/dp/OTHER{i}") for i in range(4)
        ]
        return _result(query, cards)

    _results, _candidates, lane_match, _dropped = _run(amazon, ["q1", "q2"], _matcher(seen))

    assert calls == ["q1"]
    assert lane_match.confirmed_matches


def test_with_a_single_query_there_is_nothing_to_retry():
    calls, seen = [], []

    def amazon(query):
        calls.append(query)
        return _result(query, [_cand(f"https://www.amazon.in/dp/OTHER{i}") for i in range(5)])

    _results, _candidates, lane_match, _dropped = _run(amazon, ["only"], _matcher(seen))

    assert calls == ["only"]
    assert lane_match.confirmed_matches == []


def test_a_blocked_search_is_not_retried_with_another_query():
    calls, seen = [], []

    def amazon(query):
        calls.append(query)
        return SearchScrapeResult(
            source_domain="amazon.in",
            query=query,
            candidates=[_cand(f"https://www.amazon.in/dp/OTHER{i}") for i in range(5)],
            blocked=True,
        )

    _results, _candidates, lane_match, _dropped = _run(amazon, ["q1", "q2"], _matcher(seen))

    # A CAPTCHA is not cured by different words: the retry stage owns that.
    assert calls == ["q1"]
    assert lane_match.confirmed_matches == []


def test_the_retry_does_not_verify_the_same_listing_twice():
    calls, seen = [], []

    def amazon(query):
        calls.append(query)
        shared = "https://www.amazon.in/dp/SAME"
        if query == "q1":
            return _result(query, [_cand(shared)] + [_cand(f"https://www.amazon.in/dp/OTHER{i}") for i in range(4)])
        return _result(query, [_cand(shared), _cand("https://www.amazon.in/dp/WANT1")])

    _results, _candidates, lane_match, _dropped = _run(amazon, ["q1", "q2"], _matcher(seen))

    assert calls == ["q1", "q2"]
    assert seen.count("https://www.amazon.in/dp/SAME") == 1
    assert [d["candidate_url"] for d in lane_match.confirmed_matches] == ["https://www.amazon.in/dp/WANT1"]
