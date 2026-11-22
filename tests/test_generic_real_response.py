"""SCRP-01: generic.scrape must work with a real requests.Response.

The older tests used a fake response whose .text could be overwritten, so they
never noticed that a real Response does not allow that.
"""
import requests

from scrapers import generic

PAGE = b"""<html><head><title>Logitech M650 Wireless Mouse</title>
<meta property="og:title" content="Logitech M650 Wireless Mouse">
<meta property="og:price:amount" content="1299">
<meta property="og:price:currency" content="INR">
</head><body><p>Logitech M650 Wireless Mouse - Rs. 1,299</p></body></html>"""


class _RealResponseSession:
    """Returns a REAL requests.Response, built by hand, with no network."""

    def get(self, url, headers=None, timeout=None):
        resp = requests.Response()
        resp.status_code = 200
        resp._content = PAGE
        resp.encoding = "utf-8"
        resp.url = url
        return resp


def test_generic_scrape_works_with_a_real_response_object():
    result = generic.scrape("https://www.example-shop.in/m650", session=_RealResponseSession())
    assert result.fetch_error is None
    assert result.structured_fields["title"] == "Logitech M650 Wireless Mouse"
    assert result.structured_fields["price"] == 1299.0
    assert result.structured_fields["currency"] == "INR"
    assert "Logitech M650" in result.raw_page_text


class _DocumentSession:
    def get(self, url, headers=None, timeout=None):
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"%PDF-1.4\n" + b"junk" * 1000
        resp.encoding = "utf-8"
        resp.headers["Content-Type"] = "application/pdf"
        resp.url = url
        return resp


def test_a_pdf_link_is_refused_without_becoming_a_product_page():
    result = generic.scrape("https://tender.example.in/file.pdf", session=_DocumentSession())
    assert result.fetch_error == "refused: document, not a product page"
    assert not result.structured_fields


def test_a_huge_title_is_capped_before_it_reaches_the_llm():
    long_title = "Printer " * 200
    page = f"<html><head><title>{long_title}</title></head><body>Printer</body></html>".encode()

    class Session:
        def get(self, url, headers=None, timeout=None):
            resp = requests.Response()
            resp.status_code = 200
            resp._content = page
            resp.encoding = "utf-8"
            resp.url = url
            return resp

    result = generic.scrape("https://shop.example.in/printer", session=Session())
    assert len(result.structured_fields["title"]) <= 200
