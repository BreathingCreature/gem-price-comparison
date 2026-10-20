"""Stage 6.1d: the recall safety net.

The pre-filter exists to save LLM calls, and its one unforgivable failure is
cutting the true match. These tests pin the two guarantees that prevent it:
a relevant listing survives no matter where it sits on the page, and a shop
that would send nothing still sends its three best listings.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import prefilter  # noqa: E402
from llm import match as matcher  # noqa: E402
from scrapers._base import CandidateResult  # noqa: E402

PRODUCT = {
    "canonical_name": "Stainless Steel Insulated Water Bottle Blue 1 Litre",
    "brand": None,
    "model": None,
    "key_identifiers": [],
    "gem_price": 900.0,
}

# 28 words, and it carries every word of the reference product. Long titles are
# how real Amazon listings look.
REAL_TITLE = (
    "Stainless Steel Insulated Water Bottle Blue 1 Litre Double Wall Vacuum Flask "
    "with Leak Proof Lid Keeps Drinks Hot or Cold for School Office Gym Travel and Outdoors"
)


def _cand(url, title):
    return CandidateResult(
        source_domain="amazon.in",
        url=url,
        scraper_used="x",
        structured_fields={"title": title, "price": 850.0, "currency": "INR"},
        raw_page_text="",
    )


def _page_of_forty():
    """One shop, 40 listings, the true one at position 31."""
    competitors = [_cand(f"https://www.amazon.in/dp/B{i:09d}", f"Water bottle for office {i}") for i in range(39)]
    real = _cand("https://www.amazon.in/dp/REAL000001", REAL_TITLE)
    return competitors[:30] + [real] + competitors[30:]


def test_the_true_long_title_reaches_the_llm_from_position_thirty_one():
    page = _page_of_forty()
    assert len(page) == 40 and page[30].url.endswith("REAL000001")
    assert len(REAL_TITLE.split()) == 28

    kept, dropped = prefilter.filter_candidates(page, PRODUCT, top=10)
    assert "https://www.amazon.in/dp/REAL000001" in [c.url for c in kept]
    assert len(kept) <= prefilter.MAX_NON_MODEL_PER_SHOP
    assert len(kept) + len(dropped) == 40

    seen = []

    def fake(normalized, candidate, client=None):
        seen.append(candidate["url"])
        return {
            "candidate_url": candidate["url"],
            "source_domain": candidate["source_domain"],
            "is_match": True,
            "confidence": 0.95,
            "reason": "same product",
            "price_used": 850.0,
            "extraction_source": "scraper",
        }

    import llm.match as module

    original = module.verify_or_extract
    module.verify_or_extract = fake
    try:
        result = matcher.match_candidates(PRODUCT, kept)
    finally:
        module.verify_or_extract = original

    assert "https://www.amazon.in/dp/REAL000001" in seen
    assert [d["candidate_url"] for d in result.confirmed_matches] == ["https://www.amazon.in/dp/REAL000001"]


def test_a_shop_where_nothing_scores_well_still_sends_three():
    listings = [_cand(f"https://shop.example/p/{i}", f"Mouse Pad XL gaming desk mat {i}") for i in range(9)]
    kept, dropped = prefilter.filter_candidates(listings, PRODUCT, top=10)
    assert len(kept) == prefilter.SAFETY_NET_KEEP == 3
    assert len(dropped) == 6
    assert all(d["rule"] == "not_relevant" for d in dropped)


def test_the_safety_net_does_not_fire_when_a_shop_kept_something():
    listings = [
        _cand("https://shop.example/p/ok", "Stainless Steel Insulated Water Bottle Blue 1 Litre"),
    ] + [_cand(f"https://shop.example/p/{i}", f"Mouse Pad XL gaming desk mat {i}") for i in range(9)]
    kept, _ = prefilter.filter_candidates(listings, PRODUCT, top=10)
    assert [c.url for c in kept] == ["https://shop.example/p/ok"]
