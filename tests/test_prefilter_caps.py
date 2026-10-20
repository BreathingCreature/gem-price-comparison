"""Stage 6.1: the pre-filter's hard caps, safety net and drop log.

The filter decisions here are pure functions of the product and the listing
titles, so nothing is faked beyond the CandidateResult objects themselves.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import prefilter  # noqa: E402
from scrapers._base import CandidateResult  # noqa: E402

PRODUCT = {
    "canonical_name": "Logitech M650 Wireless Mouse",
    "brand": "Logitech",
    "model": "M650",
    "key_identifiers": ["Logitech", "M650"],
    "gem_price": 2000.0,
}


def _cand(domain, url, title, price=100.0):
    return CandidateResult(
        source_domain=domain,
        url=url,
        scraper_used="x",
        structured_fields={"title": title, "price": price, "currency": "INR"},
        raw_page_text="",
    )


def _urls(candidates):
    return [c.url for c in candidates]


# --- the shop cap ---------------------------------------------------------------

def test_the_shop_cap_is_ten_non_model_listings_even_with_a_bigger_n():
    listings = [_cand("amazon.in", f"u{i}", f"Logitech wireless mouse silent variant {i}") for i in range(30)]
    kept, dropped = prefilter.filter_candidates(listings, PRODUCT, top=50)
    assert len(kept) == prefilter.MAX_NON_MODEL_PER_SHOP == 10
    assert len(dropped) == 20
    assert {d["rule"] for d in dropped} == {"over_shop_cap"}


def test_model_code_hits_are_kept_past_the_shop_cap_up_to_twenty():
    plain = [_cand("flipkart.com", f"p{i}", f"Logitech wireless mouse silent variant {i}") for i in range(12)]
    hits = [_cand("flipkart.com", f"hit{i}", f"Logitech M650 wireless mouse {i}", price=100.0 + i * 50) for i in range(25)]
    kept, dropped = prefilter.filter_candidates(plain + hits, PRODUCT, top=2)

    kept_hits = [u for u in _urls(kept) if u.startswith("hit")]
    assert len(kept_hits) == prefilter.MAX_MODEL_HITS_PER_SHOP == 20
    assert len([u for u in _urls(kept) if u.startswith("p")]) == 2
    # Above the cap the listing nearest the GeM price (2000) wins; prices run
    # 100..1300, so the five dearest model hits are the ones dropped.
    assert set(kept_hits) == {f"hit{i}" for i in range(5, 25)}
    assert {d["rule"] for d in dropped if d["url"].startswith("hit")} == {"over_model_cap"}


def test_a_shop_that_would_send_nothing_gets_its_best_three():
    listings = [
        _cand("shop.example", f"u{i}", f"Mouse Pad XL gaming desk mat {i}") for i in range(8)
    ]
    kept, dropped = prefilter.filter_candidates(listings, PRODUCT, top=10)
    assert len(kept) == prefilter.SAFETY_NET_KEEP == 3
    assert len(dropped) == 5
    # The safety net exists so an unknown product shape is never silently
    # reduced to nothing; it is not a "these are relevant" claim.
    assert {d["rule"] for d in dropped} == {"not_relevant"}


# --- the drop log ----------------------------------------------------------------

def test_every_drop_carries_the_trace_fields():
    listings = [_cand("amazon.in", f"u{i}", f"Logitech wireless mouse silent variant {i}") for i in range(12)]
    _, dropped = prefilter.filter_candidates(listings, PRODUCT, top=1)
    assert len(dropped) == 11
    for record in dropped:
        assert set(record) == {"url", "source_domain", "title", "price", "score", "tier", "rule"}
        assert record["rule"] in {"over_shop_cap", "over_model_cap", "not_relevant"}
        assert isinstance(record["score"], float)
        assert record["tier"] in {1, 2, 3}


# --- the relevance bar -----------------------------------------------------------

def test_a_product_with_no_brand_and_no_model_gets_a_lower_bar():
    vague = {"canonical_name": "Wireless Mouse", "brand": None, "model": None}
    assert prefilter.has_identity_clues(prefilter.product_tokens(PRODUCT)) is True
    assert prefilter.has_identity_clues(prefilter.product_tokens(vague)) is False
    assert prefilter.min_relevant_score(prefilter.product_tokens(PRODUCT)) == prefilter.RELEVANT_MIN_SCORE
    assert prefilter.min_relevant_score(prefilter.product_tokens(vague)) == prefilter.NO_CLUE_RELEVANT_MIN_SCORE
    assert prefilter.NO_CLUE_RELEVANT_MIN_SCORE < prefilter.RELEVANT_MIN_SCORE


def test_turning_the_filter_off_keeps_everything():
    listings = [_cand("amazon.in", f"u{i}", "Mouse Pad XL") for i in range(5)]
    kept, dropped = prefilter.filter_candidates(listings, PRODUCT, top=0)
    assert _urls(kept) == _urls(listings)
    assert dropped == []


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
