"""The cheap pre-LLM filter (core/prefilter.py): rank each shop's listings by
how much their title overlaps the product's words, keep the best N per shop.
Listings with no title are always kept — there is nothing to compare."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import prefilter  # noqa: E402
from scrapers._base import CandidateResult  # noqa: E402

PRODUCT = {
    "canonical_name": "Logitech M650 Wireless Mouse",
    "brand": "Logitech",
    "model": "M650",
    "key_identifiers": ["M650"],
}


def _cand(domain, url, title):
    return CandidateResult(source_domain=domain, url=url, scraper_used="x",
                           structured_fields={"title": title, "price": 100.0, "currency": "INR"}, raw_page_text="")


def test_similarity_is_high_for_the_same_words():
    score = prefilter.similarity("Logitech M650 Wireless Mouse", prefilter.product_tokens(PRODUCT))
    assert score > 0.7


def test_similarity_is_low_for_other_products():
    score = prefilter.similarity("Mouse Pad XL for Desk", prefilter.product_tokens(PRODUCT))
    assert score < 0.2


def test_filter_keeps_the_best_n_listings_per_shop():
    good = _cand("flipkart.com", "https://www.flipkart.com/good/p/itm1", "Logitech M650 Wireless Mouse Silent")
    bad = _cand("flipkart.com", "https://www.flipkart.com/bad/p/itm2", "Mouse Pad XL for Desk")
    kept, dropped = prefilter.filter_candidates([bad, good], PRODUCT, top=1)
    assert [c.url for c in kept] == [good.url]
    assert len(dropped) == 1
    assert dropped[0]["url"] == bad.url
    assert dropped[0]["title"] == "Mouse Pad XL for Desk"
    good_score = prefilter.similarity("Logitech M650 Wireless Mouse Silent", prefilter.product_tokens(PRODUCT))
    assert dropped[0]["score"] < good_score


def test_untitled_listings_are_always_kept():
    titled1 = _cand("flipkart.com", "u1", "Logitech M650 Wireless Mouse")
    titled2 = _cand("flipkart.com", "u2", "Mouse Pad XL for Desk")
    untitled = _cand("flipkart.com", "u3", None)
    kept, dropped = prefilter.filter_candidates([titled1, titled2, untitled], PRODUCT, top=1)
    urls = [c.url for c in kept]
    assert "u1" in urls and "u3" in urls and "u2" not in urls
    assert [d["url"] for d in dropped] == ["u2"]


def test_filter_off_keeps_everything():
    a = _cand("flipkart.com", "u1", "Mouse Pad XL for Desk")
    b = _cand("flipkart.com", "u2", "Mouse Pad XXL for Desk")
    kept, dropped = prefilter.filter_candidates([a, b], PRODUCT, top=0)
    assert [c.url for c in kept] == ["u1", "u2"]
    assert dropped == []


def test_shops_are_ranked_separately():
    fk_good = _cand("flipkart.com", "f1", "Logitech M650 Wireless Mouse")
    fk_bad = _cand("flipkart.com", "f2", "Mouse Pad XL for Desk")
    am = _cand("amazon.in", "a1", "Wireless Mouse Pad")
    kept, dropped = prefilter.filter_candidates([fk_bad, fk_good, am], PRODUCT, top=1)
    assert [c.url for c in kept] == ["f1", "a1"]  # one kept per shop
    assert [d["url"] for d in dropped] == ["f2"]


def test_key_identifiers_as_a_bare_string_are_handled():
    product = {"canonical_name": "Logitech Mouse", "brand": "Logitech", "model": None, "key_identifiers": "M650"}
    assert "m650" in prefilter.product_tokens(product)


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
