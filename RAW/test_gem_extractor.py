"""
Tests for gem_extractor.py.

Note: this sandbox can't reach mkp.gem.gov.in (network is restricted to a
fixed domain allowlist), so these tests run against hand-built mock HTML
using the same CSS classes the real selectors target, with a fake requests
session standing in for the real one. This proves the *parsing logic* is
correct. It does NOT prove the selectors match the real live page — that
still needs a run against an actual GeM URL on a machine with normal
internet access. Treat a green test run here as "the code works as
written", not "this works against the real site".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gem_extractor import (  # noqa: E402
    classify_price,
    extract_gem_product,
    parse_gem_url,
    _extract_specs,
)
from bs4 import BeautifulSoup  # noqa: E402


# --- classify_price -----------------------------------------------------------

def test_classify_price_fixed():
    assert classify_price("₹1,299") == (1299.0, "fixed")


def test_classify_price_range():
    assert classify_price("₹1,200 - ₹1,500") == (1200.0, "range")


def test_classify_price_l1_rate():
    value, ptype = classify_price("L1 rate: 899")
    assert ptype == "L1_rate"
    assert value == 899.0


def test_classify_price_unknown():
    assert classify_price("") == (None, "unknown")
    assert classify_price("Contact seller") == (None, "unknown")


# --- parse_gem_url --------------------------------------------------------------

def test_parse_gem_url_matches_known_pattern():
    url = (
        "https://mkp.gem.gov.in/computer-mouse/logitech-m650-mouse-bt-usb/"
        "p-5116877-17935427244-cat.html"
    )
    category, product = parse_gem_url(url)
    assert category == "computer-mouse"
    assert product == "logitech-m650-mouse-bt-usb"


def test_parse_gem_url_no_match_returns_empty():
    category, product = parse_gem_url("https://mkp.gem.gov.in/some/other/path")
    assert category == ""
    assert product == ""


# --- _extract_specs -------------------------------------------------------------

def test_extract_specs_from_table():
    html = """
    <table>
      <tr><td>Connectivity</td><td>Bluetooth + USB</td></tr>
      <tr><td>Colour</td><td>Graphite</td></tr>
    </table>
    """
    soup = BeautifulSoup(html, "lxml")
    specs = _extract_specs(soup)
    assert specs["Connectivity"] == "Bluetooth + USB"
    assert specs["Colour"] == "Graphite"


def test_extract_specs_from_dl():
    html = """
    <dl>
      <dt>Brand</dt><dd>Logitech</dd>
      <dt>Model</dt><dd>M650</dd>
    </dl>
    """
    soup = BeautifulSoup(html, "lxml")
    specs = _extract_specs(soup)
    assert specs["Brand"] == "Logitech"
    assert specs["Model"] == "M650"


# --- extract_gem_product (full flow, fake network) ------------------------------

MOCK_PRODUCT_HTML = """
<html><body>
  <div class="variant-desc">
    <span class="variant-title">Logitech M650 Wireless Mouse (BT + USB)</span>
  </div>
  <span class="variant-final-price">₹1,299</span>
  <span class="sold_as oem">Logitech India</span>
  <table>
    <tr><td>Connectivity</td><td>Bluetooth + USB</td></tr>
  </table>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self, html: str):
        self._html = html
        self.last_url = None

    def get(self, url, headers=None, timeout=None):
        self.last_url = url
        return _FakeResponse(self._html)


def test_extract_gem_product_full_flow():
    url = (
        "https://mkp.gem.gov.in/computer-mouse/logitech-m650-mouse-bt-usb/"
        "p-5116877-17935427244-cat.html"
    )
    fake_session = _FakeSession(MOCK_PRODUCT_HTML)

    product = extract_gem_product(url, session=fake_session)

    assert product.category_slug == "computer-mouse"
    assert product.product_slug == "logitech-m650-mouse-bt-usb"
    assert "M650" in product.title
    assert product.gem_price == 1299.0
    assert product.gem_price_type == "fixed"
    assert product.specs["Connectivity"] == "Bluetooth + USB"
    assert product.specs["seller"] == "Logitech India"
    assert product.brand == "Logitech"  # from the title heuristic


def test_extract_gem_product_missing_fields_does_not_crash():
    url = "https://mkp.gem.gov.in/some-cat/some-product/p-1-2-cat.html"
    fake_session = _FakeSession("<html><body>Nothing here</body></html>")

    product = extract_gem_product(url, session=fake_session)

    assert product.title == ""
    assert product.gem_price is None
    assert product.gem_price_type == "unknown"


if __name__ == "__main__":
    # Allow running without pytest: `python tests/test_gem_extractor.py`
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
