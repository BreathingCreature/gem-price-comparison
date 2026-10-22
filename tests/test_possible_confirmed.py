"""A same-product verdict with no price is confirmed only when the listing
itself proves the identity (model code, or brand + two product words).

Before this, such a listing stayed "possible" forever even when the page was
the manufacturer's own product page: the price gate demanded a number.
The price still stays None — unknown is reported as unknown.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import prefilter  # noqa: E402
from llm import match as matcher  # noqa: E402

matcher.MATCH_CONFIDENCE_THRESHOLD = 0.75

NDO = {
    "canonical_name": "Naina Semiconductor NDO-24 bench type dissolved oxygen meter",
    "brand": "Naina Semiconductor",
    "model": "NDO-24",
    "key_identifiers": ["NDO-24", "0 to 20 ppm"],
    "gem_price": 10000.0,
}


def _candidate(url, title="", page="", price=None, domain="nainasemi.com"):
    fields = {"title": title}
    if price is not None:
        fields["price"] = price
    return {"url": url, "source_domain": domain, "structured_fields": fields, "raw_page_text": page}


def _verdict(url, *, is_match=True, confidence=0.9, price=None, reason="same model code", domain="nainasemi.com"):
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
    queue = list(answers)

    def fake(normalized, candidate, client=None):
        answer = queue.pop(0)
        return answer(candidate) if callable(answer) else dict(answer)

    monkeypatch.setattr(matcher, "verify_or_extract", fake)
    return fake


# --- the promotion ---------------------------------------------------------------

def test_model_code_in_the_title_confirms_with_the_price_still_unknown(monkeypatch):
    cand = _candidate("https://nainasemi.com/ndo24", title="Digital Dissolved Oxygen Meter NDO24 Manufacturer")
    _fake_verify(monkeypatch, _verdict(cand["url"]))

    result = matcher.match_candidates(NDO, [cand])

    decision = result.all_decisions[0]
    assert decision["outcome"] == matcher.OUTCOME_MATCH
    assert decision["price_used"] is None  # unknown stays unknown
    assert result.confirmed_matches == [decision]
    assert result.possible_matches == []


def test_model_code_only_on_the_page_still_confirms(monkeypatch):
    cand = _candidate(
        "https://tradeindia.com/ndo-24",
        title="Digital Dissolved Oxygen Meter (lab) at Best Price",
        page="Specifications: model NDO 24, range 0-20 ppm. Ask for price.",
        domain="tradeindia.com",
    )
    _fake_verify(monkeypatch, _verdict(cand["url"], confidence=0.85, domain="tradeindia.com"))

    result = matcher.match_candidates(NDO, [cand])

    assert result.all_decisions[0]["outcome"] == matcher.OUTCOME_MATCH


def test_brand_plus_two_product_words_confirms_a_model_without_digits(monkeypatch):
    normalized = {
        "canonical_name": "Godrej Interio Maestro Main Desk ERU Pedestal and Bookcase",
        "brand": "Godrej Interio",
        "model": "Maestro Main DSK ERU PED and Bookcase",
        "key_identifiers": ["Godrej Interio", "Maestro", "1800x900 mm"],
        "gem_price": 100000.0,
    }
    cand = _candidate("https://primegodrej.com/maestro", title="Godrej Maestro Executive Suite", domain="primegodrej.com")
    _fake_verify(monkeypatch, _verdict(cand["url"], confidence=0.8, reason="same model", domain="primegodrej.com"))

    result = matcher.match_candidates(normalized, [cand])

    assert result.all_decisions[0]["outcome"] == matcher.OUTCOME_MATCH


# --- the guards ------------------------------------------------------------------

def test_a_title_with_no_model_and_one_word_stays_possible(monkeypatch):
    normalized = {
        "canonical_name": "Godrej Interio Maestro Main Desk ERU Pedestal and Bookcase",
        "brand": "Godrej Interio",
        "model": "Maestro Main DSK ERU PED and Bookcase",
        "gem_price": 100000.0,
    }
    cand = _candidate("https://example.com/suite", title="Godrej Executive Suite", domain="example.com")
    _fake_verify(monkeypatch, _verdict(cand["url"], confidence=0.8, reason="same model", domain="example.com"))

    result = matcher.match_candidates(normalized, [cand])

    assert result.all_decisions[0]["outcome"] == matcher.OUTCOME_POSSIBLE
    assert result.confirmed_matches == []


def test_a_plain_title_with_no_identity_words_stays_possible(monkeypatch):
    cand = _candidate("https://example.com/meter", title="Digital meter for laboratories", domain="example.com")
    _fake_verify(monkeypatch, _verdict(cand["url"], reason="same product name", domain="example.com"))

    result = matcher.match_candidates(NDO, [cand])

    assert result.all_decisions[0]["outcome"] == matcher.OUTCOME_POSSIBLE


def test_a_different_pack_never_promotes_even_with_the_model_code(monkeypatch):
    cand = _candidate("https://example.com/ndo24-pack", title="Naina NDO24 pack of 50", domain="example.com")
    _fake_verify(monkeypatch, _verdict(cand["url"], confidence=0.9, reason="different pack size 25 vs 50", domain="example.com"))

    result = matcher.match_candidates(NDO, [cand])

    assert result.all_decisions[0]["outcome"] == matcher.OUTCOME_POSSIBLE


def test_a_confident_no_is_not_promoted_by_the_model_code(monkeypatch):
    cand = _candidate("https://example.com/ndo24-copy", title="Naina NDO24 replacement probe", domain="example.com")
    _fake_verify(monkeypatch, _verdict(cand["url"], is_match=False, confidence=0.95, reason="different model", domain="example.com"))

    result = matcher.match_candidates(NDO, [cand])

    assert result.all_decisions[0]["outcome"] == matcher.OUTCOME_NO_MATCH


def test_confirmed_matches_sort_unknown_prices_last(monkeypatch):
    priced = _candidate("https://shop.com/ndo24", title="Naina NDO24 meter", price=8000.0, domain="shop.com")
    unknown = _candidate("https://brand.com/ndo24", title="Naina NDO24 meter product page", domain="brand.com")
    _fake_verify(
        monkeypatch,
        _verdict(unknown["url"], price=None, domain="brand.com"),
        _verdict(priced["url"], price=8000.0, domain="shop.com"),
    )

    result = matcher.match_candidates(NDO, [unknown, priced])

    assert [d["price_used"] for d in result.confirmed_matches] == [8000.0, None]


# --- the evidence helper itself ---------------------------------------------------

def test_evidence_ignores_spaces_and_dashes_in_a_model_code():
    product = prefilter.product_tokens({"canonical_name": "NDO-24 meter", "model": "NDO-24"})
    assert prefilter.identity_evidence("Meter NDO 24", "", product) is True
    assert prefilter.identity_evidence("Meter NDO24", "", product) is True
    assert prefilter.identity_evidence("Meter NDO25", "", product) is False


def test_evidence_needs_two_product_words_not_just_the_brand():
    product = prefilter.product_tokens(
        {"canonical_name": "Godrej Interio Maestro Desk", "brand": "Godrej Interio", "model": "Maestro Desk"}
    )
    assert prefilter.identity_evidence("Godrej Maestro", "", product) is True
    assert prefilter.identity_evidence("Godrej Chair", "", product) is False
