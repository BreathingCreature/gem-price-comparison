"""The generic scraper reads structured data honestly and refuses private addresses.

Brand pages in India publish schema.org/Product JSON-LD for Google; meta tags
are the older fallback, and h1/title is often all there is. A listing without
a price must come back with price=None + price_unknown instead of being dropped
or invented, and a link from a search engine must never make the process fetch
127.0.0.1 or the LAN.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapers import generic  # noqa: E402


class _Response:
    def __init__(self, text: str, url: str | None = None, status_code: int = 200):
        self.text = text
        if url is not None:
            self.url = url
        self.status_code = status_code

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, response: _Response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.response


def _scrape(html: str, url: str = "https://brandstore.example.com/p/1", redirect_to: str | None = None):
    session = _Session(_Response(html, url=redirect_to))
    result = generic.scrape(url, session=session)
    return result, session


JSONLD_PRODUCT = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Product",
 "name": "Logitech M650 Wireless Mouse",
 "image": ["https://cdn.example.com/m650.jpg"],
 "brand": {"@type": "Brand", "name": "Logitech"},
 "offers": {"@type": "Offer", "price": "1299.00", "priceCurrency": "INR",
            "availability": "https://schema.org/InStock"}}
</script>
</head><body><h1>ignored</h1></body></html>
"""


def test_jsonld_product_is_used_before_meta_tags():
    result, _ = _scrape(JSONLD_PRODUCT)
    fields = result.structured_fields
    assert fields["title"] == "Logitech M650 Wireless Mouse"
    assert fields["price"] == 1299.0
    assert fields["currency"] == "INR"
    assert fields["in_stock"] is True
    assert fields["brand"] == "Logitech"
    assert fields["image_url"] == "https://cdn.example.com/m650.jpg"


def test_jsonld_graph_and_aggregate_offer():
    html = """
    <script type="application/ld+json">
    {"@graph": [{"@type": "BreadcrumbList"}, {"@type": "Product", "name": "M650",
      "offers": {"@type": "AggregateOffer", "lowPrice": "1099", "priceCurrency": "inr"}}]}
    </script>
    """
    result, _ = _scrape(html)
    fields = result.structured_fields
    assert fields["price"] == 1099.0
    assert fields["currency"] == "INR"


def test_a_listing_without_a_price_is_kept_and_flagged():
    html = """
    <script type="application/ld+json">
    {"@type": "Product", "name": "Logitech M650"}
    </script>
    """
    result, _ = _scrape(html)
    fields = result.structured_fields
    assert fields["title"] == "Logitech M650"
    assert fields["price"] is None
    assert fields["price_unknown"] is True
    assert result.fetch_error is None


def test_h1_is_the_title_when_there_is_no_meta_or_jsonld():
    result, _ = _scrape("<html><body><h1>Logitech M650 Wireless Mouse</h1></body></html>")
    fields = result.structured_fields
    assert fields["title"] == "Logitech M650 Wireless Mouse"
    assert fields["price_unknown"] is True


def test_a_price_specification_object_is_read():
    # schema.org lets the price live in an object; str(dict) used to give None.
    html = """
    <script type="application/ld+json">
    {"@type": "Product", "name": "M650",
     "offers": {"@type": "Offer",
                "priceSpecification": {"@type": "PriceSpecification", "price": 1299, "priceCurrency": "INR"}}}
    </script>
    """
    result, _ = _scrape(html)
    fields = result.structured_fields
    assert fields["price"] == 1299.0
    assert fields["currency"] == "INR"


def test_a_foreign_currency_is_kept_as_is():
    html = """
    <script type="application/ld+json">
    {"@type": "Product", "name": "M650", "offers": {"price": "39.99", "priceCurrency": "USD"}}
    </script>
    """
    result, _ = _scrape(html)
    assert result.structured_fields["currency"] == "USD"
    assert result.structured_fields.get("currency_assumed") is None


def test_a_soft_404_page_is_flagged():
    result, _ = _scrape("<html><head><title>Page Not Found | Brand</title></head><body></body></html>")
    assert result.structured_fields["page_not_found"] is True


# --- SSRF guard -----------------------------------------------------------------------

def _assert_refused(url: str):
    class _ExplodingSession:
        def get(self, *args, **kwargs):
            raise AssertionError("a private or non-http URL must not be fetched")

    result = generic.scrape(url, session=_ExplodingSession())
    assert result.fetch_error and result.fetch_error.startswith("refused:")


def test_private_addresses_are_refused():
    _assert_refused("http://127.0.0.1/admin")
    _assert_refused("http://localhost:8000/api")
    _assert_refused("http://192.168.1.5/router")
    _assert_refused("http://169.254.169.254/latest/meta-data")
    _assert_refused("http://[::1]/")


def test_non_http_schemes_are_refused():
    _assert_refused("file:///C:/Windows/win.ini")
    _assert_refused("ftp://example.com/file")


def test_a_redirect_to_a_private_address_is_refused():
    session = _Session(_Response("<html></html>", url=None))
    session.response.url = "http://127.0.0.1/secret"
    result = generic.scrape("https://brandstore.example.com/p/1", session=session)
    assert result.fetch_error == "refused: redirect to a private or non-http address"


def test_a_public_redirect_is_followed():
    session = _Session(_Response("<html><h1>M650</h1></html>", url="https://brandstore.example.com/p/2"))
    result = generic.scrape("https://brandstore.example.com/p/1", session=session)
    assert result.fetch_error is None
    assert result.structured_fields["title"] == "M650"
