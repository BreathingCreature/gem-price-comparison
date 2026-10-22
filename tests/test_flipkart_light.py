"""Flipkart's fast path: one plain HTTP page before Chrome is allowed to start.

The fast path must (a) accept a real results page and never start Chrome,
(b) hand a wall/shell page to Chrome, (c) keep page-1 results when a later
page is odd, and (d) rebuild a dead Chrome once per run, not once per query.
"""
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapers import flipkart  # noqa: E402
from scrapers._base import SearchScrapeResult  # noqa: E402


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


def _card(index: int) -> str:
    return f"""
    <div class="nZIRY7"><a href="/logitech-m650-{index}/p/itm{index}?pid=MOU{index}">
      <div class="RG5Slk">Logitech M650 Wireless Mouse ({index})</div>
      <div class="hZ3P6w DeU9vF">₹1,29{index}</div>
    </a></div>
    """


LIGHT_PAGE = "<html><body>" + "".join(_card(i) for i in range(1, 5)) + "</body></html>"
SHELL_PAGE = "<html><body><div>Please log in to continue</div></body></html>"


class _FakeDriver:
    page_source = "<html></html>"

    def quit(self):
        pass


class _FakeCurlResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


def _curl_stub(response):
    """The lazy `from curl_cffi import requests` inside _fetch_light_page."""
    requests_stub = type(sys)("curl_cffi.requests")
    requests_stub.get = lambda url, **kwargs: response
    package_stub = type(sys)("curl_cffi")
    package_stub.requests = requests_stub
    return package_stub


# The autouse conftest fixture replaces the module attribute with a stub so no
# test can reach the network through libcurl; the real function is captured
# here at import time, before any fixture runs.
_REAL_FETCH_LIGHT = flipkart._fetch_light_page


# --- the real fetch -------------------------------------------------------------------


def test_fetch_light_page_returns_a_200_body(monkeypatch):
    monkeypatch.setitem(sys.modules, "curl_cffi", _curl_stub(_FakeCurlResponse("<html>real page</html>")))
    assert _REAL_FETCH_LIGHT("logitech m650") == "<html>real page</html>"


def test_fetch_light_page_refuses_a_wall(monkeypatch):
    monkeypatch.setitem(sys.modules, "curl_cffi", _curl_stub(_FakeCurlResponse("<html>Please verify you are a human</html>")))
    assert _REAL_FETCH_LIGHT("logitech m650") is None


def test_fetch_light_page_gives_up_on_a_non_200(monkeypatch):
    monkeypatch.setitem(sys.modules, "curl_cffi", _curl_stub(_FakeCurlResponse("<html>forbidden</html>", status_code=403)))
    assert _REAL_FETCH_LIGHT("logitech m650") is None


# --- the decision ---------------------------------------------------------------------


def test_a_real_results_page_is_used_without_starting_chrome():
    def no_chrome(*args, **kwargs):
        raise AssertionError("Chrome must not start for a usable fast-path page")

    with _patched(flipkart, build_stealth_driver=no_chrome,
                  _fetch_light_page=lambda query, page=1: LIGHT_PAGE):
        result = flipkart.search("logitech m650", max_pages=1)
    assert result.status == "ok"
    assert len(result.candidates) == 4


def test_a_shell_page_falls_back_to_chrome():
    built = []

    def build(**kwargs):
        built.append(kwargs)
        return _FakeDriver()

    with _patched(flipkart, build_stealth_driver=build,
                  _fetch_light_page=lambda query, page=1: SHELL_PAGE,
                  _load_search_page=lambda driver, query, page=1: LIGHT_PAGE):
        result = flipkart.search("logitech m650", max_pages=1)
    assert built, "the shell page must send the search to Chrome"
    assert result.status == "ok"
    assert len(result.candidates) == 4


def test_a_later_page_failure_keeps_page_1_results():
    def fetch(query, page=1):
        return LIGHT_PAGE if page == 1 else None

    result = flipkart._search_light("logitech m650", max_pages=3, fetch_page=fetch)
    assert result is not None
    assert len(result.candidates) == 4
    assert result.pages_scraped == 1


def test_a_page_1_failure_sends_the_whole_search_to_chrome():
    assert flipkart._search_light("logitech m650", max_pages=3,
                                  fetch_page=lambda query, page=1: None) is None


# --- dead Chrome is rebuilt once ------------------------------------------------------

def test_a_dead_driver_is_rebuilt_once_and_the_query_retried():
    builds = []
    calls = []

    def build_driver():
        builds.append(object())
        return _FakeDriver()

    outcomes = [
        SearchScrapeResult(source_domain="flipkart.com", query="q", candidates=[], blocked=False,
                           pages_scraped=1, error="WebDriverException: invalid session id", status=None),
        SearchScrapeResult(source_domain="flipkart.com", query="q", candidates=[], blocked=False,
                           pages_scraped=1, error=None, status="ok"),
    ]

    def fake_search(driver, query, **kwargs):
        calls.append(driver)
        return outcomes.pop(0)

    with _patched(flipkart, _search_with_driver=fake_search,
                  _fetch_light_page=lambda query, page=1: None):
        browser = flipkart.FlipkartBrowser(build_driver=build_driver)
        try:
            result = browser.search("q", max_pages=1)
        finally:
            browser.close()

    assert len(builds) == 2
    assert calls[0] is not calls[1]
    assert result.status == "ok"


def test_a_second_death_is_not_rebuilt_again():
    builds = []

    def build_driver():
        builds.append(object())
        return _FakeDriver()

    dead = SearchScrapeResult(source_domain="flipkart.com", query="q", candidates=[], blocked=False,
                              pages_scraped=1, error="no such window: target window already closed", status=None)

    with _patched(flipkart, _search_with_driver=lambda driver, query, **kwargs: dead,
                  _fetch_light_page=lambda query, page=1: None):
        browser = flipkart.FlipkartBrowser(build_driver=build_driver)
        try:
            result = browser.search("q", max_pages=1)
        finally:
            browser.close()

    assert len(builds) == 2  # one rebuild, not one per retry
    assert result.error is not None
