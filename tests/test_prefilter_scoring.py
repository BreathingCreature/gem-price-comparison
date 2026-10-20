"""The pre-LLM filter must never cut the right listing just because its title is long.
Old scoring (Jaccard) gave a long real Amazon title 0.19 and a wrong short title 0.33."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import prefilter  # noqa: E402
from scrapers._base import CandidateResult  # noqa: E402

M221 = {
    "canonical_name": "Logitech M221 Wireless Mouse",
    "brand": "Logitech",
    "model": "M221",
    "key_identifiers": ["Logitech", "M221", "wireless"],
}
LONG_REAL_TITLE = (
    "Logitech M221 Silent Wireless Mouse, 2.4GHz with USB Receiver, 1000 DPI, 3 Buttons, "
    "Compatible with Windows Mac Chrome OS Laptop PC Charcoal Grey"
)


def _cand(domain, url, title):
    return CandidateResult(source_domain=domain, url=url, scraper_used="x",
                           structured_fields={"title": title, "price": 100.0, "currency": "INR"}, raw_page_text="")


def test_a_long_real_title_scores_higher_than_a_short_wrong_one():
    product = prefilter.product_tokens(M221)
    long_real = prefilter.similarity(LONG_REAL_TITLE, product)
    assert long_real >= 0.9
    assert long_real > prefilter.similarity("HP X200 Wireless Mouse", product)
    assert long_real > prefilter.similarity("Logitech MK215 Wireless Keyboard and Mouse Combo", product)


def test_long_real_title_is_relevant_and_a_pad_is_not():
    product = prefilter.product_tokens(M221)
    assert prefilter.relevant_title(LONG_REAL_TITLE, product) is True
    assert prefilter.relevant_title("Mouse Pad Large Gaming", product) is False
    assert prefilter.relevant_title("HP X200 Wireless Mouse", product) is False


def test_the_real_long_title_survives_a_page_of_short_wrong_titles():
    wrong = [_cand("amazon.in", f"u{i}", f"Wireless Mouse brand{i} 2.4GHz") for i in range(25)]
    real = _cand("amazon.in", "REAL", LONG_REAL_TITLE)
    page = wrong[:10] + [real] + wrong[10:]
    kept, dropped = prefilter.filter_candidates(page, M221, top=8)
    # Stage 6: the real title carries the model code, so it is kept no matter
    # how many short titles surround it; the short ones do not clear the 0.4
    # relevant bar for a product with a brand AND a model.
    assert [c.url for c in kept] == ["REAL"]
    assert len(dropped) == 25
    assert {d["rule"] for d in dropped} == {"not_relevant"}
    assert all("tier" in d for d in dropped)


def test_model_code_matches_with_dashes_and_spaces():
    product = prefilter.product_tokens(M221)
    assert prefilter.tier("Logitech M-221 mouse", product) == 1
    assert prefilter.tier("Logitech M 221 mouse", product) == 1
    assert prefilter.tier("Logitech mouse", product) == 2
    assert prefilter.tier("Dell mouse", product) == 3


def test_model_hits_beyond_n_are_still_kept_but_capped():
    hits = [_cand("flipkart.com", f"hit{i}", f"Cover for Logitech M221 case {i}") for i in range(30)]
    kept, dropped = prefilter.filter_candidates(hits, M221, top=2)
    # Stage 6: one absolute per-shop ceiling, not top * factor.
    assert len(kept) == prefilter.MAX_MODEL_HITS_PER_SHOP == 20
    assert len(dropped) == 30 - len(kept)
    assert {d["rule"] for d in dropped} == {"over_model_cap"}


def test_generic_or_guessed_brand_is_ignored():
    generic = prefilter.product_tokens({"canonical_name": "Office Chair", "brand": "Generic", "model": None})
    assert generic.brand_words == set()
    guessed = prefilter.product_tokens({"canonical_name": "Office Chair", "brand": "Zebronics", "model": None})
    assert guessed.brand_words == set()
    assert prefilter.tier("Office Chair black", generic) == 3


def test_brand_alias_is_accepted():
    product = prefilter.product_tokens({"canonical_name": "HP 15s Laptop", "brand": "HP", "model": "15s-fq5007TU"})
    assert prefilter.tier("Hewlett Packard 15s laptop", product) == 2
    assert prefilter.tier("HP 15s FQ5007TU 15.6 inch", product) == 1


def test_a_short_model_without_digit_is_not_a_model_key():
    product = prefilter.product_tokens({"canonical_name": "Pro Mouse", "brand": None, "model": "Pro"})
    assert product.model_keys == []
