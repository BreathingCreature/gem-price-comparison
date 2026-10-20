"""GeM products carry a price object (low/high/kind), not just one number.

A range listing must stay a range downstream, an L1 price must be labelled,
and the trace must show which selector produced the number. Brand/model come
from the spec table when the title doesn't have them.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapers.gem import GemProduct, classify_price_range, extract_gem_product  # noqa: E402

GEM_URL = (
    "https://gem.gov.in/catalog/wireless-mouse/logitech-m650-wireless-mouse-"
    "abc123?source=search"
)


class _Response:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, html: str):
        self.html = html

    def get(self, url, **kwargs):
        return _Response(self.html)


def _page(price_html: str, title: str = "Logitech M650 Wireless Mouse", specs: str = "") -> str:
    title_block = f'<div class="variant-desc"><div class="variant-title">{title}</div></div>' if title else ""
    return f"""
    <html><body>
    {title_block}
    {price_html}
    {specs}
    </body></html>
    """


SPECS = """
<table>
  <tr><td>Brand</td><td>Logitech</td></tr>
  <tr><td>Model</td><td>M650</td></tr>
</table>
"""


# --- classify_price_range -------------------------------------------------------------

def test_a_fixed_price_has_equal_ends():
    assert classify_price_range("₹1,299.00") == {"low": 1299.0, "high": 1299.0, "kind": "fixed"}


def test_a_range_keeps_both_ends():
    assert classify_price_range("₹1,299 - ₹1,499") == {"low": 1299.0, "high": 1499.0, "kind": "range"}


def test_an_l1_price_is_labelled_l1():
    assert classify_price_range("L1 ₹999") == {"low": 999.0, "high": 999.0, "kind": "l1"}


def test_no_price_is_unknown():
    assert classify_price_range("") == {"low": None, "high": None, "kind": "unknown"}


# --- extraction ------------------------------------------------------------------------

def test_the_price_object_and_its_selector_reach_the_product():
    product = extract_gem_product(GEM_URL, session=_Session(_page('<div class="final-price">₹1,299.00</div>', specs=SPECS)))
    assert product.price == {"low": 1299.0, "high": 1299.0, "kind": "fixed"}
    assert product.price_selector == ".final-price"
    assert product.to_dict()["price"]["kind"] == "fixed"
    assert product.gem_price == 1299.0  # legacy field stays the low end


def test_a_range_page_keeps_its_high_end():
    page = _page('<div class="final-price">₹1,299 - ₹1,499</div>', specs=SPECS)
    product = extract_gem_product(GEM_URL, session=_Session(page))
    assert product.price == {"low": 1299.0, "high": 1499.0, "kind": "range"}
    assert product.gem_price == 1299.0


def test_a_placeholder_price_element_is_skipped():
    page = _page('<div class="final-price">₹</div><div class="our_price">₹1,499</div>', specs=SPECS)
    product = extract_gem_product(GEM_URL, session=_Session(page))
    assert product.gem_price == 1499.0
    assert product.price_selector == ".our_price"


def test_model_comes_from_the_spec_table_when_the_title_lacks_it():
    product = extract_gem_product(GEM_URL, session=_Session(_page('<div class="final-price">₹1,299</div>', specs=SPECS)))
    assert product.model == "M650"


def test_brand_comes_from_the_spec_table_when_there_is_no_title():
    page = _page('<div class="final-price">₹999</div>', title="", specs=SPECS)
    product = extract_gem_product(GEM_URL, session=_Session(page))
    assert product.brand == "Logitech"


def test_legacy_construction_still_gets_a_price_object():
    product = GemProduct(
        url=GEM_URL, category_slug="c", product_slug="p", title="T",
        brand=None, model=None, specs={}, gem_price=500.0, gem_price_type="L1_rate",
    )
    assert product.to_dict()["price"] == {"low": 500.0, "high": 500.0, "kind": "l1"}
