"""
Tests for scrapers/flipkart.py, scrapers/amazon.py, scrapers/generic.py.

flipkart.parse_cards_from_html and amazon.parse_cards_from_html are pure
functions (html string in, list[dict] out) — deliberately separated from
the Selenium driving code so they're testable without Chrome installed,
which this sandbox doesn't have anyway. generic.scrape() is tested with a
fake requests session, same pattern as Part 1's tests.

None of this proves the selectors match today's real Flipkart/Amazon
markup — only that the parsing logic does the right thing with HTML shaped
like it. Flipkart's shape came from tested code; Amazon's is unverified
(see the warning at the top of scrapers/amazon.py).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapers import flipkart, amazon, generic  # noqa: E402


# --- Flipkart --------------------------------------------------------------------

FLIPKART_MOCK_HTML = """
<html><body>
<div class="nZIRY7">
  <a href="/logitech-m650-wireless-mouse/p/itm1a2b3c4d5e?pid=MOUXYZ123">
    <div class="RG5Slk">Logitech M650 Wireless Mouse (Graphite)</div>
    <div class="hZ3P6w DeU9vF">₹1,299</div>
  </a>
</div>
<div class="nZIRY7">
  <a href="/logitech-mx-master-3s/p/itm9z8y7x6w5v?pid=MOUABC987">
    <div class="RG5Slk">Logitech MX Master 3S</div>
    <div class="hZ3P6w DeU9vF">₹8,995</div>
  </a>
</div>
</body></html>
"""


def test_flipkart_parse_cards_basic():
    cards = flipkart.parse_cards_from_html(FLIPKART_MOCK_HTML)
    assert len(cards) == 2
    assert cards[0]["name"] == "Logitech M650 Wireless Mouse (Graphite)"
    assert cards[0]["price"] == "1299"
    assert cards[0]["link"].startswith("https://www.flipkart.com/logitech-m650")


def test_flipkart_parse_cards_dedupes():
    dup_html = FLIPKART_MOCK_HTML + FLIPKART_MOCK_HTML
    cards = flipkart.parse_cards_from_html(dup_html)
    assert len(cards) == 2  # not 4 — duplicates collapsed


def test_flipkart_parse_cards_empty_on_no_match():
    cards = flipkart.parse_cards_from_html("<html><body>no products here</body></html>")
    assert cards == []


# --- Amazon ------------------------------------------------------------------------

AMAZON_MOCK_HTML = """
<html><body>
<div data-component-type="s-search-result" data-asin="B0DEMO1234">
  <h2><a href="/Logitech-M650-Wireless-Mouse/dp/B0DEMO1234">
    <span class="a-text-normal">Logitech M650 Wireless Mouse</span>
  </a></h2>
  <span class="a-price"><span class="a-offscreen">₹1,199.00</span></span>
</div>
<div data-component-type="s-search-result">
  <!-- sponsored placeholder, no data-asin — should be skipped -->
  <h2><span class="a-text-normal">Sponsored junk</span></h2>
</div>
</body></html>
"""


def test_amazon_parse_cards_basic():
    cards = amazon.parse_cards_from_html(AMAZON_MOCK_HTML)
    assert len(cards) == 1
    assert cards[0]["name"] == "Logitech M650 Wireless Mouse"
    assert cards[0]["price"] == "1199.00"
    assert cards[0]["link"].startswith("https://www.amazon.in/Logitech-M650")


def test_amazon_parse_cards_skips_no_asin():
    only_placeholder = """
    <div data-component-type="s-search-result">
      <h2><span class="a-text-normal">No asin here</span></h2>
    </div>
    """
    assert amazon.parse_cards_from_html(only_placeholder) == []


# --- Generic ------------------------------------------------------------------------

GENERIC_MOCK_HTML = """
<html><head>
  <meta property="og:title" content="Logitech M650 Wireless Mouse - BrandStore" />
  <meta property="og:price:amount" content="1349" />
  <meta property="og:image" content="https://example.com/m650.jpg" />
</head><body>
  <p>Some visible product description text.</p>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, html: str):
        self._html = html

    def get(self, url, headers=None, timeout=None):
        return _FakeResponse(self._html)


def test_generic_scrape_reads_og_tags():
    result = generic.scrape("https://brandstore.example.com/m650", session=_FakeSession(GENERIC_MOCK_HTML))
    assert result.structured_fields["title"] == "Logitech M650 Wireless Mouse - BrandStore"
    assert result.structured_fields["price"] == 1349.0
    assert result.structured_fields["image_url"] == "https://example.com/m650.jpg"
    assert "product description" in result.raw_page_text
    assert result.fetch_error is None


def test_generic_scrape_no_meta_falls_back_to_raw_text_only():
    html = "<html><body><p>Some page with no og tags, price is Rs 999 somewhere.</p></body></html>"
    result = generic.scrape("https://unknown.example.com/x", session=_FakeSession(html))
    assert result.structured_fields is None
    assert "Rs 999" in result.raw_page_text


def test_generic_scrape_handles_fetch_error():
    class _BrokenSession:
        def get(self, url, headers=None, timeout=None):
            raise ConnectionError("boom")

    result = generic.scrape("https://down.example.com", session=_BrokenSession())
    assert result.structured_fields is None
    assert result.raw_page_text is None
    assert "boom" in result.fetch_error


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
