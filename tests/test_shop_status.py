"""Shops must report what went wrong and must not hand back an MRP as the price.

Amazon, Flipkart and GeM now carry status/reason (ok, blocked, parser_empty)
and per-card stock, so a run can say "CAPTCHA" or "layout changed" instead of
reporting either as "no results" or "cheapest at ₹X" (the struck-out MRP).
"""
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapers import amazon, flipkart  # noqa: E402


@contextmanager
def _patched(obj, **attrs):
    old = {name: getattr(obj, name) for name in attrs}
    for name, value in attrs.items():
        setattr(obj, name, value)
    try:
        yield
    finally:
        for name, value in old.items():
            setattr(obj, name, value)


class _FakeDriver:
    page_source = "<html><body>fake</body></html>"

    def quit(self):
        pass


def _no_sleeps(module, fn, *args, **kwargs):
    with _patched(module.time, sleep=lambda seconds: None):
        return fn(*args, **kwargs)


# --- Amazon ---------------------------------------------------------------------------


AMAZON_SELLING_AND_MRP = """
<div data-component-type="s-search-result" data-asin="B0DEMO1234">
  <h2><a href="/Logitech-M650/dp/B0DEMO1234"><span class="a-text-normal">Logitech M650</span></a></h2>
  <span class="a-price a-text-price"><span class="a-offscreen">₹1,999.00</span></span>
  <span class="a-price"><span class="a-offscreen">₹1,199.00</span></span>
</div>
"""


def test_amazon_uses_the_selling_price_not_the_struck_out_mrp():
    cards = amazon.parse_cards_from_html(AMAZON_SELLING_AND_MRP)
    assert len(cards) == 1
    assert cards[0]["price"] == "1199.00"  # not 1999 — .a-text-price is the MRP


def test_amazon_card_out_of_stock():
    html = """
    <div data-component-type="s-search-result" data-asin="B0DEMO5678">
      <h2><a href="/X/dp/B0DEMO5678"><span class="a-text-normal">Logitech M650</span></a></h2>
      <span class="a-price"><span class="a-offscreen">₹1,199.00</span></span>
      <div>Currently unavailable.</div>
    </div>
    """
    cards = amazon.parse_cards_from_html(html)
    assert cards[0]["in_stock"] is False
    candidates = []
    amazon._collect_cards(cards, candidates, set(), 10)
    assert candidates[0].structured_fields["in_stock"] is False


def test_amazon_zero_cards_without_a_no_results_marker_is_parser_empty():
    with _patched(
        amazon,
        _search_curl_cffi=lambda query, max_results, max_pages: None,
        build_stealth_driver=lambda *a, **k: _FakeDriver(),
        _load_search_page=lambda driver, query, page=1: "<html><body>shell</body></html>",
    ):
        result = _no_sleeps(amazon, amazon.search, "logitech m650", max_pages=1)
    assert result.status == "parser_empty"
    assert result.blocked is False
    assert result.reason == "the page had no product cards"


def test_amazon_genuine_no_results_is_ok():
    no_results = "<html><body><div>No results for 'zzzz'</div></body></html>"
    with _patched(
        amazon,
        _search_curl_cffi=lambda query, max_results, max_pages: None,
        build_stealth_driver=lambda *a, **k: _FakeDriver(),
        _load_search_page=lambda driver, query, page=1: no_results,
    ):
        result = _no_sleeps(amazon, amazon.search, "zzzz", max_pages=1)
    assert result.status == "ok"
    assert result.candidates == []
    assert result.reason == "Amazon reported no results"


def test_amazon_captcha_page_is_blocked_with_a_reason():
    with _patched(
        amazon,
        _search_curl_cffi=lambda query, max_results, max_pages: None,
        build_stealth_driver=lambda *a, **k: _FakeDriver(),
        _load_search_page=lambda driver, query, page=1: None,
    ):
        result = _no_sleeps(amazon, amazon.search, "logitech m650", max_pages=1)
    assert result.status == "blocked"
    assert result.reason == "captcha or robot check"


# --- Flipkart -------------------------------------------------------------------------


def test_flipkart_mrp_is_never_the_price_when_the_selling_class_drifted():
    html = """
    <div class="nZIRY7"><a href="/logitech-m650/p/itm1?pid=MOU1">
      <div class="RG5Slk">Logitech M650</div>
      <div class="_3I9_wc">₹1,999</div>
      <div class="_1vC4OE">₹1,299</div>
    </a></div>
    """
    cards = flipkart.parse_cards_from_html(html)
    assert cards[0]["price"] == "1299"


def test_flipkart_card_out_of_stock():
    html = """
    <div class="nZIRY7"><a href="/logitech-m650/p/itm1?pid=MOU1">
      <div class="RG5Slk">Logitech M650</div>
      <div class="hZ3P6w DeU9vF">₹1,299</div>
      <div>Sold Out</div>
    </a></div>
    """
    cards = flipkart.parse_cards_from_html(html)
    assert cards[0]["in_stock"] is False


def test_flipkart_zero_cards_without_a_no_results_marker_is_parser_empty():
    with _patched(
        flipkart,
        _fetch_light_page=lambda query, page=1: None,
        _load_search_page=lambda driver, query, page=1: "<html><body>shell</body></html>",
    ):
        result = _no_sleeps(flipkart, flipkart._search_with_driver, _FakeDriver(), "logitech m650", max_pages=1)
    assert result.status == "parser_empty"
    assert result.blocked is False
    assert result.reason == "the page had no product cards"


def test_flipkart_no_results_page_is_ok():
    no_results = "<html><body><div>No products found for this search</div></body></html>"
    with _patched(
        flipkart,
        _fetch_light_page=lambda query, page=1: None,
        _load_search_page=lambda driver, query, page=1: no_results,
    ):
        result = _no_sleeps(flipkart, flipkart._search_with_driver, _FakeDriver(), "zzzz", max_pages=1)
    assert result.status == "ok"
    assert result.candidates == []
    assert result.reason == "Flipkart reported no results"


def test_flipkart_captcha_page_is_blocked_with_a_reason():
    with _patched(
        flipkart,
        _fetch_light_page=lambda query, page=1: None,
        _load_search_page=lambda driver, query, page=1: None,
    ):
        result = _no_sleeps(flipkart, flipkart._search_with_driver, _FakeDriver(), "logitech m650", max_pages=1)
    assert result.status == "blocked"
    assert result.reason == "captcha or robot check"
