"""Stage 6.3/6.4: one outcome per judged listing, verdict reuse, and the
scraper's price as the price.

The verifier itself is monkeypatched, so these tests say nothing about the
prompt — only about what match_candidates does with the answers.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm import match as matcher  # noqa: E402
from llm.router import GatewayBusy  # noqa: E402

matcher.MATCH_CONFIDENCE_THRESHOLD = 0.75


def _candidate(url, domain="flipkart.com", title=None, price=None):
    fields = {"title": title or f"listing {url}"}
    if price is not None:
        fields["price"] = price
    return {"url": url, "source_domain": domain, "structured_fields": fields, "raw_page_text": ""}


def _verdict(url, *, is_match=True, confidence=0.9, price=1000.0, reason="same model", domain="flipkart.com"):
    return {
        "candidate_url": url,
        "source_domain": domain,
        "is_match": is_match,
        "confidence": confidence,
        "reason": reason,
        "price_used": price,
        "extraction_source": "llm",
    }


def _fake_verify(monkeypatch, *answers):
    """answers: dicts (returned in call order) or callables(candidate)."""
    calls = []
    queue = list(answers)

    def fake(normalized, candidate, client=None):
        calls.append(candidate["url"])
        answer = queue.pop(0)
        return answer(candidate) if callable(answer) else dict(answer)

    import llm.match as module

    monkeypatch.setattr(module, "verify_or_extract", fake)
    return calls


# --- outcomes --------------------------------------------------------------------

def test_a_confirmed_listing_with_a_price_is_a_match(monkeypatch):
    _fake_verify(monkeypatch, _verdict("https://x.com/a", price=1200.0))
    result = matcher.match_candidates({"canonical_name": "Demo"}, [_candidate("https://x.com/a")])

    decision = result.all_decisions[0]
    assert decision["outcome"] == matcher.OUTCOME_MATCH
    assert "why" not in decision
    assert [d["candidate_url"] for d in result.confirmed_matches] == ["https://x.com/a"]


def test_a_different_pack_is_possible_with_a_plain_reason(monkeypatch):
    _fake_verify(
        monkeypatch,
        _verdict(
            "https://x.com/a",
            is_match=False,
            confidence=0.85,
            price=900.0,
            reason="same mouse, different pack size (2-pack vs single)",
        ),
    )
    result = matcher.match_candidates({"canonical_name": "Demo"}, [_candidate("https://x.com/a")])

    decision = result.all_decisions[0]
    assert decision["outcome"] == matcher.OUTCOME_POSSIBLE
    assert decision["why"] == "different pack"
    assert result.confirmed_matches == []
    assert [d["candidate_url"] for d in result.possible_matches] == ["https://x.com/a"]


def test_a_hesitant_answer_between_half_and_the_threshold_is_possible(monkeypatch):
    _fake_verify(
        monkeypatch,
        _verdict("https://x.com/a", is_match=False, confidence=0.6, price=900.0, reason="probably not"),
    )
    result = matcher.match_candidates({"canonical_name": "Demo"}, [_candidate("https://x.com/a")])

    decision = result.all_decisions[0]
    assert decision["outcome"] == matcher.OUTCOME_POSSIBLE
    assert decision["why"] == "confidence 0.60 below 0.75"


def test_a_confident_no_is_no_match(monkeypatch):
    _fake_verify(
        monkeypatch,
        _verdict("https://x.com/a", is_match=False, confidence=0.2, price=900.0, reason="different product"),
    )
    result = matcher.match_candidates({"canonical_name": "Demo"}, [_candidate("https://x.com/a")])

    assert result.all_decisions[0]["outcome"] == matcher.OUTCOME_NO_MATCH
    assert result.confirmed_matches == [] and result.possible_matches == []


def test_a_gateway_failure_is_not_checked_not_rejected(monkeypatch):
    def boom(normalized, candidate, client=None):
        raise GatewayBusy("every endpoint busy")

    import llm.match as module

    monkeypatch.setattr(module, "verify_or_extract", boom)
    result = matcher.match_candidates({"canonical_name": "Demo"}, [_candidate("https://x.com/a")])

    assert result.all_decisions == []
    assert result.skipped == ["https://x.com/a"]  # old callers still see "no verdict"
    assert result.not_checked == [{"url": "https://x.com/a", "reason": "every endpoint busy"}]


def test_possible_matches_are_sorted_with_known_prices_first(monkeypatch):
    _fake_verify(
        monkeypatch,
        _verdict("https://x.com/unknown", is_match=True, confidence=0.9, price=None),
        _verdict("https://x.com/dear", is_match=False, confidence=0.8, price=1500.0, reason="different pack"),
        _verdict("https://x.com/cheap", is_match=False, confidence=0.8, price=700.0, reason="different pack"),
    )
    candidates = [
        _candidate("https://x.com/unknown"),
        _candidate("https://x.com/dear"),
        _candidate("https://x.com/cheap"),
    ]
    result = matcher.match_candidates({"canonical_name": "Demo"}, candidates)

    assert [d["candidate_url"] for d in result.possible_matches] == [
        "https://x.com/cheap",
        "https://x.com/dear",
        "https://x.com/unknown",
    ]


# --- verdict reuse ---------------------------------------------------------------

def test_identical_title_and_price_asks_the_llm_once(monkeypatch):
    calls = _fake_verify(monkeypatch, _verdict("https://x.com/a"))
    candidates = [
        _candidate("https://x.com/a", domain="flipkart.com", title="Same Mouse", price=1000.0),
        _candidate("https://x.com/b", domain="amazon.in", title="Same Mouse", price=1000.0),
    ]
    result = matcher.match_candidates({"canonical_name": "Demo"}, candidates)

    assert calls == ["https://x.com/a"]
    assert len(result.all_decisions) == 2
    reused = result.all_decisions[1]
    assert reused["reused_verdict"] is True
    assert reused["candidate_url"] == "https://x.com/b" and reused["source_domain"] == "amazon.in"


def test_a_run_can_share_its_verdict_cache_between_shops(monkeypatch):
    calls = _fake_verify(monkeypatch, _verdict("https://x.com/a"))
    shared = {}
    for url, domain in (("https://x.com/a", "flipkart.com"), ("https://x.com/b", "amazon.in")):
        matcher.match_candidates(
            {"canonical_name": "Demo"},
            [_candidate(url, domain=domain, title="Same Mouse", price=1000.0)],
            verdict_cache=shared,
        )
    assert calls == ["https://x.com/a"]
    assert len(shared) == 1


def test_with_reuse_off_every_listing_is_asked_about(monkeypatch):
    monkeypatch.setattr(matcher, "REUSE_VERDICTS", False)
    calls = _fake_verify(monkeypatch, _verdict("https://x.com/a"), _verdict("https://x.com/b"))
    candidates = [
        _candidate("https://x.com/a", title="Same Mouse", price=1000.0),
        _candidate("https://x.com/b", title="Same Mouse", price=1000.0),
    ]
    matcher.match_candidates({"canonical_name": "Demo"}, candidates)
    assert calls == ["https://x.com/a", "https://x.com/b"]


# --- price truth -----------------------------------------------------------------

def test_the_scraper_price_wins_and_the_llm_price_stays_in_the_trace(monkeypatch):
    _fake_verify(monkeypatch, _verdict("https://x.com/a", price=999.0))
    result = matcher.match_candidates(
        {"canonical_name": "Demo"}, [_candidate("https://x.com/a", price=1200.0)]
    )

    decision = result.all_decisions[0]
    assert decision["price_used"] == 1200.0
    assert decision["price_from"] == "scraper"
    assert decision["llm_price_seen"] == 999.0
    assert "1200" in decision["price_note"] and "999" in decision["price_note"]


def test_without_a_scraper_price_the_llm_price_is_kept(monkeypatch):
    _fake_verify(monkeypatch, _verdict("https://x.com/a", price=999.0))
    result = matcher.match_candidates({"canonical_name": "Demo"}, [_candidate("https://x.com/a")])

    decision = result.all_decisions[0]
    assert decision["price_used"] == 999.0
    assert "price_from" not in decision


def test_the_batch_result_serializes_the_new_lists(monkeypatch):
    _fake_verify(monkeypatch, _verdict("https://x.com/a", is_match=True, confidence=0.9, price=None))
    result = matcher.match_candidates({"canonical_name": "Demo"}, [_candidate("https://x.com/a")])
    payload = result.to_dict()
    assert payload["possible_matches"] and payload["not_checked"] == []
    assert payload["all_decisions"][0]["outcome"] == matcher.OUTCOME_POSSIBLE
