"""Saved pages and fake drivers for the scraper start, retry and cleanup paths.

Every page here is a small hand-built fixture: no test opens a socket. The
fixtures cover what a live page looks like when it is blocked, empty, or only
partly usable, which is where the parsers used to guess.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from selenium.common.exceptions import TimeoutException

from scrapers import _base as base
from scrapers import amazon, flipkart

PAGES = Path(__file__).resolve().parent / "fixtures" / "pages"


def page(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


class _Element:
    def __init__(self, text: str = ""):
        self.text = text


class _FakeDriver:
    def __init__(self, html: str = "", url: str = "https://www.amazon.in/s?k=x", elements=None, get_error=None):
        self.page_source = html
        self.current_url = url
        self._elements = [object()] * 5 if elements is None else elements
        self._get_error = get_error
        self.got = []
        self.scripts = []
        self.quit_calls = 0

    def get(self, url):
        self.got.append(url)
        if self._get_error is not None:
            raise self._get_error

    def find_elements(self, by, selector):
        return self._elements

    def find_element(self, by, selector):
        return _Element(self.page_source)

    def execute_script(self, script):
        self.scripts.append(script)

    def quit(self):
        self.quit_calls += 1


class _BrokenBodyDriver:
    current_url = "https://shop.test/search"
    page_source = "<html><body>clean</body></html>"

    def find_element(self, by, selector):
        raise RuntimeError("page is gone")


class _ExplodingScrollDriver:
    def execute_script(self, script):
        raise RuntimeError("renderer gone")

    def find_elements(self, by, selector):
        return []


class _EnoughCardsDriver:
    def __init__(self):
        self.calls = 0
        self.last_script = ""

    def execute_script(self, script):
        self.calls += 1
        self.last_script = script

    def find_elements(self, by, selector):
        return [object()] * 60


def _no_sleep(monkeypatch, module):
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)


# --- shared block detection and scrolling ------------------------------------------


def test_the_saved_captcha_pages_are_detected_by_the_shared_guard():
    for name in ("amazon-captcha.html", "flipkart-captcha.html"):
        driver = _FakeDriver(html=page(name))
        assert base.detect_block(driver) is True, name


def test_a_partly_usable_page_is_not_mistaken_for_a_block():
    assert base.detect_block(_FakeDriver(html=page("amazon-partial.html"))) is False
    assert base.detect_block(_FakeDriver(html=page("flipkart-partial.html"))) is False


def test_detect_block_reads_the_url_and_survives_a_broken_driver():
    assert base.detect_block(_FakeDriver(url="https://www.amazon.in/errors/validateCaptcha")) is True
    assert base.detect_block(_BrokenBodyDriver()) is False


def test_scroll_to_load_more_stops_once_enough_items_are_visible(monkeypatch):
    driver = _EnoughCardsDriver()
    _no_sleep(monkeypatch, base)
    base.scroll_to_load_more(driver)
    assert driver.calls == 1
    assert driver.last_script.startswith("window.scrollTo")


def test_scroll_to_load_more_gives_up_when_the_page_breaks(monkeypatch):
    _no_sleep(monkeypatch, base)
    base.scroll_to_load_more(_ExplodingScrollDriver())


# --- driver start -------------------------------------------------------------------


def test_build_stealth_driver_registers_the_driver_it_started(monkeypatch):
    from selenium import webdriver

    seen = {}

    class Chrome:
        def __init__(self, options=None):
            self.options = options
            self.timeouts = []
            self.cdp = []
            self.quit_calls = 0

        def set_page_load_timeout(self, seconds):
            self.timeouts.append(seconds)

        def set_script_timeout(self, seconds):
            self.timeouts.append(seconds)

        def execute_cdp_cmd(self, name, payload):
            self.cdp.append((name, payload))

        def quit(self):
            self.quit_calls += 1

    monkeypatch.setattr(webdriver, "Chrome", Chrome)

    driver = base.build_stealth_driver(light=True)
    try:
        assert isinstance(driver, Chrome)
        assert driver.timeouts == [60, 30]
        assert driver.cdp[0][0] == "Page.addScriptToEvaluateOnNewDocument"
        assert "webdriver" in driver.cdp[0][1]["source"]
        assert base._LIVE_DRIVERS[id(driver)] is driver
    finally:
        base.forget_driver(driver)


def test_build_stealth_driver_quits_and_raises_when_setup_fails(monkeypatch):
    from selenium import webdriver

    class Chrome:
        def __init__(self, options=None):
            self.quit_calls = 0

        def set_page_load_timeout(self, seconds):
            raise RuntimeError("browser went away")

        def quit(self):
            self.quit_calls += 1

    monkeypatch.setattr(webdriver, "Chrome", Chrome)

    with pytest.raises(RuntimeError, match="browser went away"):
        base.build_stealth_driver()

    assert all(not isinstance(d, Chrome) for d in base._LIVE_DRIVERS.values())


# --- page loading -------------------------------------------------------------------


def test_amazon_load_page_returns_a_saved_page(monkeypatch):
    driver = _FakeDriver(html=page("amazon-partial.html"))
    _no_sleep(monkeypatch, amazon)

    html = amazon._load_search_page(driver, "usb c cable", 1)

    assert html == page("amazon-partial.html")
    assert driver.got == ["https://www.amazon.in/s?k=usb+c+cable&page=1"]


def test_amazon_load_page_returns_none_for_the_saved_captcha_page(monkeypatch):
    driver = _FakeDriver(html=page("amazon-captcha.html"))
    _no_sleep(monkeypatch, amazon)

    assert amazon._load_search_page(driver, "usb c cable", 1) is None


def test_amazon_load_page_keeps_the_page_after_a_slow_navigation(monkeypatch):
    driver = _FakeDriver(html=page("amazon-partial.html"), get_error=TimeoutException("slow"))
    sleeps = []
    monkeypatch.setattr(amazon.time, "sleep", sleeps.append)

    html = amazon._load_search_page(driver, "usb c cable", 1)

    assert html == page("amazon-partial.html")
    assert len(sleeps) == 2 and 4.0 <= sleeps[0] <= 6.0


def test_flipkart_load_page_scrolls_and_returns_the_page(monkeypatch):
    driver = _FakeDriver(html=page("flipkart-partial.html"), elements=[object()] * 5)
    scrolled = []
    sleeps = []
    monkeypatch.setattr(flipkart, "_scroll_to_load_cards", lambda d: scrolled.append(d))
    monkeypatch.setattr(flipkart.time, "sleep", sleeps.append)

    html = flipkart._load_search_page(driver, "usb c cable", 1)

    assert html == page("flipkart-partial.html")
    assert scrolled == [driver]
    assert len(sleeps) == 1 and 2.0 <= sleeps[0] <= 3.0


def test_flipkart_load_page_returns_none_for_the_saved_captcha_page(monkeypatch):
    driver = _FakeDriver(html=page("flipkart-captcha.html"), elements=[object()] * 5)
    scrolled = []
    monkeypatch.setattr(flipkart, "_scroll_to_load_cards", lambda d: scrolled.append(d))
    _no_sleep(monkeypatch, flipkart)

    assert flipkart._load_search_page(driver, "usb c cable", 1) is None
    assert scrolled == []


# --- saved-page parsing -------------------------------------------------------------


def test_amazon_saved_partial_page_keeps_only_complete_cards():
    cards = amazon.parse_cards_from_html(page("amazon-partial.html"))
    assert [c["name"] for c in cards] == [
        "Logitech M650 Wireless Mouse",
        "Card with a price but no anchor",
        "Logitech K380 Keyboard",
    ]
    assert [c["price"] for c in cards] == ["1299.00", "499.00", "2199.00"]
    assert cards[1]["link"] == "https://www.amazon.in/dp/B0PART0003"
    assert all(c["link"].startswith("https://www.amazon.in/dp/") for c in cards)


def test_amazon_saved_empty_page_has_no_cards_and_is_a_genuine_answer():
    assert amazon.parse_cards_from_html(page("amazon-empty.html")) == []
    assert amazon._genuine_no_results(page("amazon-empty.html")) is True


def test_flipkart_saved_partial_page_dedupes_and_drops_incomplete_cards():
    cards = flipkart.parse_cards_from_html(page("flipkart-partial.html"))
    assert len(cards) == 1
    assert cards[0]["name"] == "Logitech M650 Wireless Mouse"
    assert cards[0]["price"] == "1299"
    assert cards[0]["link"] == "https://www.flipkart.com/logitech-m650/p/itm1?pid=MOU1"


def test_flipkart_saved_legacy_page_reads_anchor_only_cards():
    cards = flipkart.parse_cards_from_html(page("flipkart-legacy.html"))
    assert [c["name"] for c in cards] == ["Legacy Layout Mouse"]
    assert cards[0]["price"] == "649"
    assert cards[0]["link"] == "https://www.flipkart.com/legacy-mouse/p/itm9?pid=LEG1"


def test_flipkart_saved_empty_page_has_no_cards_and_is_a_genuine_answer():
    assert flipkart.parse_cards_from_html(page("flipkart-empty.html")) == []
    assert flipkart._genuine_no_results(page("flipkart-empty.html")) is True


# --- Amazon fast path ---------------------------------------------------------------


def _patched_fetch(monkeypatch, function):
    monkeypatch.setattr(amazon, "_fetch_curl_cffi_page", function)
    _no_sleep(monkeypatch, amazon)


def test_amazon_fast_path_page_one_failure_is_inconclusive(monkeypatch):
    _patched_fetch(monkeypatch, lambda query, page_no=1: None)
    assert amazon._search_curl_cffi("usb c cable", max_results=15, max_pages=2) is None


def test_amazon_fast_path_retries_a_zero_card_shell_once(monkeypatch):
    calls = []

    def fetch(query, page_no=1):
        calls.append(page_no)
        return "<html><body>shell</body></html>" if len(calls) == 1 else page("amazon-partial.html")

    _patched_fetch(monkeypatch, fetch)
    result = amazon._search_curl_cffi("logitech m650", max_results=15, max_pages=1)

    assert calls == [1, 1]
    assert result is not None and len(result.candidates) == 3


def test_amazon_fast_path_zero_cards_after_the_retry_is_inconclusive(monkeypatch):
    saved = []
    _patched_fetch(monkeypatch, lambda query, page_no=1: "<html><body>shell</body></html>")
    monkeypatch.setattr(amazon, "_save_debug", lambda html, prefix: saved.append(prefix))

    assert amazon._search_curl_cffi("usb c cable", max_results=15, max_pages=1) is None
    assert saved == ["amazon_curl_suspect"]


def test_amazon_fast_path_keeps_page_one_when_a_later_page_fails(monkeypatch):
    def fetch(query, page_no=1):
        return page("amazon-partial.html") if page_no == 1 else None

    _patched_fetch(monkeypatch, fetch)
    result = amazon._search_curl_cffi("logitech m650", max_results=15, max_pages=2)

    assert result is not None
    assert len(result.candidates) == 3
    assert result.pages_scraped == 1


def test_amazon_fast_path_accepts_a_genuine_no_results_page(monkeypatch):
    _patched_fetch(monkeypatch, lambda query, page_no=1: page("amazon-empty.html"))
    result = amazon._search_curl_cffi("zzzz", max_results=15, max_pages=1)

    assert result is not None
    assert result.candidates == [] and result.blocked is False
    assert result.reason == "Amazon reported no results"


def test_amazon_fast_path_with_no_pages_is_inconclusive(monkeypatch):
    _patched_fetch(monkeypatch, lambda query, page_no=1: page("amazon-partial.html"))
    assert amazon._search_curl_cffi("usb c cable", max_results=15, max_pages=0) is None


def test_amazon_fetch_reports_a_missing_curl_cffi(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "curl_cffi", None)
    assert amazon._fetch_curl_cffi_page("usb c cable", 1) is None


def test_amazon_fetch_gives_up_after_every_fingerprint_errors(monkeypatch):
    import sys
    import types

    attempts = []

    class Session:
        def __init__(self, impersonate=None, **kwargs):
            self.impersonate = impersonate

        def get(self, url, **kwargs):
            attempts.append(self.impersonate)
            raise OSError("connection reset")

    requests_stub = types.ModuleType("curl_cffi.requests")
    requests_stub.Session = Session
    package_stub = types.ModuleType("curl_cffi")
    package_stub.requests = requests_stub
    monkeypatch.setitem(sys.modules, "curl_cffi", package_stub)
    monkeypatch.setattr(amazon, "_sessions", {})

    assert amazon._fetch_curl_cffi_page("usb c cable", 1) is None
    assert attempts == list(amazon._CURL_IMPERSONATE)


# --- Flipkart fast path and browser -------------------------------------------------


def _flipkart_cards(titles):
    cards = "".join(
        '<div class="nZIRY7"><a href="/p/itm{i}?pid=PID{i}">'
        '<div class="RG5Slk">{title}</div><div class="hZ3P6w DeU9vF">₹{price}</div>'
        "</a></div>".format(i=i, title=title, price=1000 + i)
        for i, title in enumerate(titles)
    )
    return f"<html><body>{cards}</body></html>"


def _product():
    from core import prefilter

    return prefilter.product_tokens(
        {"canonical_name": "Logitech M650", "brand": "Logitech", "model": "M650", "key_identifiers": ["M650"]}
    )


def test_flipkart_light_stops_at_enough_relevant_cards(monkeypatch):
    pages = []

    def fetch(query, page_no=1):
        pages.append(page_no)
        return _flipkart_cards([f"Logitech M650 Mouse {page_no}-{i}" for i in range(5)])

    _no_sleep(monkeypatch, flipkart)
    result = flipkart._search_light("logitech m650", max_pages=3, product=_product(), fetch_page=fetch)

    assert pages == [1]
    assert result is not None and len(result.candidates) == 5


def test_flipkart_light_stops_when_a_later_page_is_not_a_results_page(monkeypatch):
    pages = []

    def fetch(query, page_no=1):
        pages.append(page_no)
        return _flipkart_cards([f"Logitech M650 Mouse {i}" for i in range(3)]) if page_no == 1 else "<html><body>shell</body></html>"

    _no_sleep(monkeypatch, flipkart)
    result = flipkart._search_light("logitech m650", max_pages=3, fetch_page=fetch)

    assert pages == [1, 2]
    assert result is not None and len(result.candidates) == 3


def test_flipkart_light_stops_on_a_page_with_no_cards(monkeypatch):
    _no_sleep(monkeypatch, flipkart)
    result = flipkart._search_light("zzzz", max_pages=2, fetch_page=lambda query, page_no=1: page("flipkart-empty.html"))

    assert result is not None and result.candidates == []
    assert result.pages_scraped == 1
    assert result.reason == "Flipkart reported no results"


def test_flipkart_light_skips_duplicates_and_stops_at_the_limit(monkeypatch):
    html = _flipkart_cards(["Logitech M650 Mouse A", "Logitech M650 Mouse B", "Logitech M650 Mouse C"])
    html = html.replace("</body>", '<div class="nZIRY7"><a href="/p/itm0?pid=PID0">'
                        '<div class="RG5Slk">Logitech M650 Mouse A</div>'
                        '<div class="hZ3P6w DeU9vF">₹1000</div></a></div></body>')
    _no_sleep(monkeypatch, flipkart)
    result = flipkart._search_light("logitech m650", max_results=1, max_pages=2, fetch_page=lambda query, page_no=1: html)

    assert result is not None
    assert [c.structured_fields["title"] for c in result.candidates] == ["Logitech M650 Mouse A"]


def test_flipkart_browser_uses_the_light_page_without_starting_chrome(monkeypatch):
    expected = base.SearchScrapeResult(source_domain="flipkart.com", query="q", candidates=[])
    started = []
    monkeypatch.setattr(flipkart, "FLIPKART_LIGHT", True)
    monkeypatch.setattr(flipkart, "_search_light", lambda *a, **k: expected)
    browser = flipkart.FlipkartBrowser(build_driver=lambda: _FakeDriver())
    monkeypatch.setattr(browser, "start", lambda: started.append(True))

    assert browser.search("q") is expected
    assert started == []
    browser.close()


def test_flipkart_browser_rebuilds_after_search_raises_a_dead_driver_error(monkeypatch):
    builds = []
    calls = []

    def build():
        builds.append(object())
        return _FakeDriver()

    def search(driver, query, **kwargs):
        calls.append(driver)
        if len(calls) == 1:
            raise RuntimeError("invalid session id")
        return base.SearchScrapeResult(source_domain="flipkart.com", query=query, candidates=[], status="ok")

    monkeypatch.setattr(flipkart, "_search_with_driver", search)
    monkeypatch.setattr(flipkart, "_fetch_light_page", lambda query, page=1: None)
    browser = flipkart.FlipkartBrowser(build_driver=build)
    try:
        result = browser.search("q", max_pages=1)
    finally:
        browser.close()

    assert len(builds) == 2 and calls[0] is not calls[1]
    assert result.status == "ok"


def test_flipkart_browser_close_warns_when_quit_fails(caplog):
    class StuckDriver(_FakeDriver):
        def quit(self):
            raise RuntimeError("still open")

    browser = flipkart.FlipkartBrowser(build_driver=lambda: StuckDriver())
    browser.start()
    browser.close()

    assert "did not close cleanly" in caplog.text


def test_flipkart_reloads_a_page_that_rendered_zero_cards(monkeypatch):
    loaded = []

    def load(driver, query, page_no=1):
        loaded.append(page_no)
        if page_no == 1:
            return page("flipkart-legacy.html")
        if loaded.count(2) == 1:
            return "<html><body>shell</body></html>"
        return page("flipkart-partial.html")

    monkeypatch.setattr(flipkart, "_load_search_page", load)
    _no_sleep(monkeypatch, flipkart)
    result = flipkart._search_with_driver(_FakeDriver(), "logitech m650", max_pages=2)

    assert loaded == [1, 2, 2]
    assert result.status == "ok" and len(result.candidates) == 2


def test_flipkart_reload_that_hits_a_wall_is_blocked(monkeypatch):
    loaded = []

    def load(driver, query, page_no=1):
        loaded.append(page_no)
        if page_no == 1:
            return page("flipkart-legacy.html")
        return "<html><body>shell</body></html>" if loaded.count(2) == 1 else None

    monkeypatch.setattr(flipkart, "_load_search_page", load)
    _no_sleep(monkeypatch, flipkart)
    result = flipkart._search_with_driver(_FakeDriver(), "logitech m650", max_pages=2)

    assert loaded == [1, 2, 2]
    assert result.status == "blocked" and result.blocked is True


def test_flipkart_search_failure_is_reported_not_raised(monkeypatch):
    def load(driver, query, page_no=1):
        raise RuntimeError("driver disappeared")

    monkeypatch.setattr(flipkart, "_load_search_page", load)
    result = flipkart._search_with_driver(_FakeDriver(), "logitech m650", max_pages=1)

    assert result.status is None
    assert result.error is not None and "driver disappeared" in result.error


def test_flipkart_blocked_page_still_reports_when_the_debug_copy_fails(monkeypatch, tmp_path):
    bad_dir = tmp_path / "not-a-folder"
    bad_dir.write_text("x", encoding="utf-8")
    monkeypatch.setattr(flipkart, "DEBUG_DIR", bad_dir)
    monkeypatch.setattr(flipkart, "_load_search_page", lambda driver, query, page=1: None)

    result = flipkart._search_with_driver(_FakeDriver(), "logitech m650", max_pages=1)

    assert result.status == "blocked" and result.blocked is True
