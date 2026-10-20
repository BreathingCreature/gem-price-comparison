"""
Tests for scrapers/flipkart.py, scrapers/amazon.py, scrapers/generic.py.

flipkart.parse_cards_from_html and amazon.parse_cards_from_html are pure
functions (html string in, list[dict] out) — deliberately separated from
the Selenium driving code so they're testable without Chrome installed,
which this sandbox doesn't have anyway. generic.scrape() is tested with a
fake requests session, same pattern as the GeM extractor tests.

None of this proves the selectors match today's real Flipkart/Amazon
markup — only that the parsing logic does the right thing with HTML shaped
like it. Flipkart's shape came from tested code; Amazon's is unverified
(see the warning at the top of scrapers/amazon.py).
"""
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapers import flipkart, amazon, generic, _base  # noqa: E402
from scrapers._base import parse_indian_price  # noqa: E402
from core import prefilter  # noqa: E402


# --- parse_indian_price (base) ------------------------------------------------------------

def test_parse_indian_price_rupee_forms():
    assert parse_indian_price("₹1,299") == "1299"
    assert parse_indian_price("₹1,299.00") == "1299.00"
    assert parse_indian_price("₹1,499-₹1,999") == "1499"


def test_parse_indian_price_comma_grouped_without_rupee():
    assert parse_indian_price("1,499.00") == "1499.00"


def test_parse_indian_price_does_not_read_spec_digits_as_price():
    # Live bug: bare \d+ fallback read "3" from "i3" when the price
    # selector drifted and code fell back to full card text.
    assert parse_indian_price("Intel Core i3 1215U 16GB RAM") is None
    assert parse_indian_price("16GB 512GB SSD") is None


def test_parse_indian_price_empty_and_none():
    assert parse_indian_price("") is None
    assert parse_indian_price(None) is None


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
    assert cards[0]["link"].startswith("https://www.amazon.in/dp/B0DEMO1234")


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
    """Read-only .text, like a real requests.Response."""

    def __init__(self, text: str, status: int = 200):
        self._text = text
        self.status_code = status

    @property
    def text(self) -> str:
        return self._text

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


def test_generic_scrape_domain_strips_single_www_prefix_only():
    # str.replace("www.", "") also strips mid-host: "awww.com" -> "a.com".
    result = generic.scrape("https://www.awww.com/x", session=_FakeSession(GENERIC_MOCK_HTML))
    assert result.source_domain == "awww.com"


# --- Flipkart scroll loop (driver faked; no Chrome needed) --------------------------


@contextmanager
def _patched(obj, **attrs):
    """Temporary attribute swaps — works with and without pytest's fixture."""
    old = {name: getattr(obj, name) for name in attrs}
    for name, value in attrs.items():
        setattr(obj, name, value)
    try:
        yield
    finally:
        for name, value in old.items():
            setattr(obj, name, value)


class _FakeScrollDriver:
    """Enough of a Selenium driver for a scroll loop: every scrollTo()
    moves to the next entry in link_counts, and find_elements reports that
    many 'product links'."""

    def __init__(self, link_counts):
        self.link_counts = list(link_counts)
        self.scrolls = 0

    def execute_script(self, script):
        if script.startswith("window.scrollTo"):
            self.scrolls += 1

    def find_elements(self, by, selector):
        index = min(max(self.scrolls - 1, 0), len(self.link_counts) - 1)
        return [object()] * self.link_counts[index]


class _QuietDriver:
    def quit(self):
        pass


def _call_silenced(module, fn, *args, **kwargs):
    with _patched(module.time, sleep=lambda seconds: None):
        return fn(*args, **kwargs)


def _call_recording_sleeps(module, fn, *args, **kwargs):
    sleeps = []
    with _patched(module.time, sleep=lambda seconds: sleeps.append(seconds)):
        result = fn(*args, **kwargs)
    return result, sleeps


def test_scroll_stops_after_two_rounds_without_new_cards():
    # The old loop scrolled all 8 rounds whenever the card count stayed
    # under 50 (almost always true) — two stale rounds mean the page is done.
    driver = _FakeScrollDriver([10, 10, 10, 10, 10, 10, 10, 10])
    _call_silenced(flipkart, flipkart._scroll_to_load_cards, driver)
    assert driver.scrolls == 3


def test_scroll_keeps_going_while_new_cards_keep_arriving():
    driver = _FakeScrollDriver([10, 18, 26, 30, 30, 30, 30, 30])
    _call_silenced(flipkart, flipkart._scroll_to_load_cards, driver)
    assert driver.scrolls == 6


def test_scroll_gives_up_quickly_when_the_page_is_already_full():
    driver = _FakeScrollDriver([80, 80])
    _call_silenced(flipkart, flipkart._scroll_to_load_cards, driver)
    assert driver.scrolls == 1


# --- no sleep after the last page -----------------------------------------------------

def _flipkart_search_without_browser(html, **search_kwargs):
    options = {"max_pages": 1}
    options.update(search_kwargs)
    with _patched(
        flipkart,
        build_stealth_driver=lambda *a, **k: _QuietDriver(),
        _load_search_page=lambda driver, query, page=1: html,
    ):
        return _call_recording_sleeps(flipkart, flipkart.search, "logitech m650", **options)


def test_flipkart_does_not_sleep_after_the_last_page():
    result, sleeps = _flipkart_search_without_browser(FLIPKART_MOCK_HTML)
    assert len(result.candidates) == 2
    assert sleeps == []


def test_flipkart_waits_between_pages():
    result, sleeps = _flipkart_search_without_browser(FLIPKART_MOCK_HTML, max_pages=2)
    assert len(result.candidates) == 2  # page 2 was all duplicates
    assert len(sleeps) == 1


def test_flipkart_skips_the_sleep_once_it_has_enough():
    result, sleeps = _flipkart_search_without_browser(FLIPKART_MOCK_HTML, max_results=2, max_pages=3)
    assert len(result.candidates) == 2
    assert sleeps == []


# --- C6: page 2/3 only when page 1 had too few relevant cards --------------------------


def _flipkart_html(titles):
    cards = "".join(
        '<div class="nZIRY7"><a href="/p/itm{i}?pid=PID{i}">'
        '<div class="RG5Slk">{t}</div><div class="hZ3P6w DeU9vF">₹{p}</div></a></div>'.format(
            i=i, t=t, p=1500 + i
        )
        for i, t in enumerate(titles)
    )
    return f"<html><body>{cards}</body></html>"


def _flipkart_search_with_pages(pages_html, **search_kwargs):
    seen_pages = []

    def loader(driver, query, page=1):
        seen_pages.append(page)
        return pages_html.get(page, "<html></html>")

    with _patched(
        flipkart,
        build_stealth_driver=lambda *a, **k: _QuietDriver(),
        _load_search_page=loader,
    ):
        result, sleeps = _call_recording_sleeps(flipkart, flipkart.search, "logitech m650", **search_kwargs)
    return result, sleeps, seen_pages


def _m650_product():
    return prefilter.product_tokens(
        {"canonical_name": "Logitech M650", "brand": "Logitech", "model": "M650", "key_identifiers": ["M650"]}
    )


def test_flipkart_stops_paging_once_page_1_has_enough_relevant_cards():
    # C6: five cards that pass the relevance bar on page 1 mean pages 2/3
    # cannot add much — skipping them saves 10-30 s on easy products.
    pages = {
        1: _flipkart_html([f"Logitech M650 Mouse variant {i}" for i in range(5)]),
        2: _flipkart_html(["Logitech M650 Mouse extra"]),
        3: _flipkart_html(["Logitech M650 Mouse more"]),
    }
    result, sleeps, seen = _flipkart_search_with_pages(pages, max_pages=3, product=_m650_product())
    assert seen == [1]
    assert sleeps == []
    assert len(result.candidates) == 5


def test_flipkart_keeps_paging_when_page_1_has_no_relevant_cards():
    # Junk cards must not end the pagination: the real listing often sits on
    # page 2 (the old loop counted any 15 cards toward "enough").
    pages = {
        1: _flipkart_html([f"Random Gadget {i}" for i in range(6)]),
        2: _flipkart_html([f"Logitech M650 Mouse {i}" for i in range(5)]),
        3: _flipkart_html(["Logitech M650 Mouse extra"]),
    }
    result, sleeps, seen = _flipkart_search_with_pages(pages, max_pages=3, product=_m650_product())
    assert seen == [1, 2]
    assert len(sleeps) == 1  # only between page 1 and page 2
    assert len(result.candidates) == 11


def test_flipkart_pages_as_before_without_the_product():
    pages = {
        1: _flipkart_html([f"Logitech M650 Mouse {i}" for i in range(6)]),
        2: _flipkart_html(["Anything here"]),
        3: _flipkart_html(["More things"]),
    }
    _, _, seen = _flipkart_search_with_pages(pages, max_pages=3)
    assert seen == [1, 2, 3]


# --- GEM_FLIPKART_LIGHT travels from config to the driver -----------------------------

def test_flipkart_light_switch_reaches_the_driver():
    seen = {}

    def capture(*, light=False):
        seen["light"] = light
        return _QuietDriver()

    with _patched(flipkart, build_stealth_driver=capture, FLIPKART_LIGHT=True,
                  _load_search_page=lambda driver, query, page=1: "<html></html>"):
        flipkart.search("logitech m650", max_pages=1)
    assert seen["light"] is True


def test_flipkart_is_light_by_default():
    seen = {}

    def capture(*, light=False):
        seen["light"] = light
        return _QuietDriver()

    with _patched(flipkart, build_stealth_driver=capture,
                  _load_search_page=lambda driver, query, page=1: "<html></html>"):
        flipkart.search("logitech m650", max_pages=1)
    assert seen["light"] is True


def test_flipkart_light_can_be_switched_off():
    seen = {}

    def capture(*, light=False):
        seen["light"] = light
        return _QuietDriver()

    with _patched(flipkart, build_stealth_driver=capture, FLIPKART_LIGHT=False,
                  _load_search_page=lambda driver, query, page=1: "<html></html>"):
        flipkart.search("logitech m650", max_pages=1)
    assert seen["light"] is False


def test_light_browser_options_disable_images_and_stop_at_the_dom():
    opts = _base._plain_chrome_options(light=True)
    assert "--blink-settings=imagesEnabled=false" in opts.arguments
    assert opts.page_load_strategy == "eager"


def test_default_browser_options_are_unchanged():
    opts = _base._plain_chrome_options(light=False)
    assert "--blink-settings=imagesEnabled=false" not in opts.arguments
    assert opts.page_load_strategy == "normal"


# --- one Chrome for a whole run (Flipkart Tier 2 items 1 and 2) ------------------------

class _CountingDriver:
    """A driver that only counts its quits — enough for lifecycle tests."""

    def __init__(self):
        self.quit_calls = 0

    def quit(self):
        self.quit_calls += 1


def test_flipkart_browser_reuses_one_driver_for_every_query():
    drivers = []

    def factory():
        driver = _CountingDriver()
        drivers.append(driver)
        return driver

    browser = flipkart.FlipkartBrowser(build_driver=factory)
    with _patched(flipkart, _load_search_page=lambda driver, query, page=1: FLIPKART_MOCK_HTML):
        first = _call_silenced(flipkart, browser.search, "logitech m650", max_pages=1)
        second = _call_silenced(flipkart, browser.search, "logitech wireless mouse", max_pages=1)
        browser.close()
        browser.close()
    assert len(first.candidates) == 2 and len(second.candidates) == 2
    assert len(drivers) == 1
    assert drivers[0].quit_calls == 1


def test_flipkart_browser_starts_building_before_the_first_search():
    started = threading.Event()
    release = threading.Event()

    def factory():
        started.set()
        release.wait(5)
        return _QuietDriver()

    browser = flipkart.FlipkartBrowser(build_driver=factory)
    browser.start()
    assert started.wait(5)  # the build began without waiting for a search
    release.set()
    with _patched(flipkart, _load_search_page=lambda driver, query, page=1: FLIPKART_MOCK_HTML):
        result = _call_silenced(flipkart, browser.search, "logitech m650", max_pages=1)
    assert len(result.candidates) == 2
    browser.close()


def test_flipkart_browser_close_quits_even_a_slow_build():
    driver = _CountingDriver()

    def factory():
        time.sleep(0.05)
        return driver

    browser = flipkart.FlipkartBrowser(build_driver=factory)
    browser.start()
    browser.close()
    assert driver.quit_calls == 1


def test_flipkart_browser_search_reports_a_failed_start():
    def factory():
        raise RuntimeError("no chrome today")

    browser = flipkart.FlipkartBrowser(build_driver=factory)
    browser.start()
    try:
        browser.search("logitech m650")
    except RuntimeError as e:
        assert "no chrome today" in str(e)
    else:
        raise AssertionError("search should raise the start error")
    browser.close()  # nothing was built; must not raise


def test_flipkart_module_search_still_builds_and_quits_its_own_driver():
    drivers = []

    def factory(*a, **k):
        driver = _CountingDriver()
        drivers.append(driver)
        return driver

    with _patched(flipkart, build_stealth_driver=factory,
                  _load_search_page=lambda driver, query, page=1: FLIPKART_MOCK_HTML):
        result = flipkart.search("logitech m650", max_pages=1)
    assert len(result.candidates) == 2
    assert len(drivers) == 1
    assert drivers[0].quit_calls == 1


# --- Amazon sleep trims ----------------------------------------------------------------

def _amazon_browser_search(html, **search_kwargs):
    options = {"max_pages": 1}
    options.update(search_kwargs)
    with _patched(
        amazon,
        _search_curl_cffi=lambda query, max_results, max_pages: None,
        build_stealth_driver=lambda *a, **k: _QuietDriver(),
        _load_search_page=lambda driver, query, page=1: html,
    ):
        return _call_recording_sleeps(amazon, amazon.search, "logitech m650", **options)


def test_amazon_does_not_sleep_after_the_last_page():
    result, sleeps = _amazon_browser_search(AMAZON_MOCK_HTML)
    assert len(result.candidates) == 1
    assert sleeps == []


def test_amazon_waits_between_pages():
    result, sleeps = _amazon_browser_search(AMAZON_MOCK_HTML, max_pages=2)
    assert len(result.candidates) == 1  # page 2 was all duplicates
    assert len(sleeps) == 1


def test_amazon_fast_path_skips_the_sleep_once_it_has_enough():
    with _patched(amazon, _fetch_curl_cffi_page=lambda query, page=1: AMAZON_MOCK_HTML):
        result, sleeps = _call_recording_sleeps(
            amazon, amazon._search_curl_cffi, "logitech m650", max_results=1, max_pages=3
        )
    assert result is not None and len(result.candidates) == 1
    assert sleeps == []


# --- B4 + E2: page fetches in parallel, one curl session per run -----------------------

def test_amazon_fetches_page_1_and_page_2_at_the_same_time():
    barrier = threading.Barrier(2, timeout=5.0)

    def fake_fetch(query, page=1):
        barrier.wait()  # both page fetches must be in flight together
        return AMAZON_MOCK_HTML

    with _patched(amazon, _fetch_curl_cffi_page=fake_fetch):
        result = amazon._search_curl_cffi("logitech m650", max_results=15, max_pages=2)

    assert result is not None
    assert len(result.candidates) == 1
    assert result.pages_scraped == 2  # the prefetched page 2 was used, not dropped


def test_amazon_prefetched_pages_cost_no_politeness_sleep():
    with _patched(amazon, _fetch_curl_cffi_page=lambda query, page=1: AMAZON_MOCK_HTML):
        result, sleeps = _call_recording_sleeps(
            amazon, amazon._search_curl_cffi, "logitech m650", max_results=15, max_pages=2
        )
    assert result is not None and result.pages_scraped == 2
    assert sleeps == []  # both pages left together; there is no second request to pace


class _FakeCurlResponse:
    def __init__(self, text: str, status_code: int = 200):
        self._text = text
        self.status_code = status_code

    @property
    def text(self) -> str:
        return self._text


class _FakeCurlSession:
    """Shaped like the parts of a curl_cffi Session that amazon.py uses."""
    made: list = []          # every session built, in order
    blocked_for: tuple = ()  # fingerprints whose requests come back blocked

    def __init__(self, impersonate=None, **kwargs):
        self.impersonate = impersonate
        self.gets: list = []
        _FakeCurlSession.made.append(self)

    def get(self, url, **kwargs):
        self.gets.append(url)
        if self.impersonate in _FakeCurlSession.blocked_for:
            return _FakeCurlResponse("<html>Sorry, we just need to make sure you're not a robot.</html>")
        return _FakeCurlResponse(AMAZON_MOCK_HTML)


def _forbidden_new_connection(*args, **kwargs):
    raise AssertionError("a new connection was opened instead of reusing the curl session")


_MISSING = object()


@contextmanager
def _stub_curl_cffi():
    """Stand-in for the curl_cffi package the fetch function imports lazily."""
    requests_stub = type(sys)("curl_cffi.requests")
    requests_stub.Session = _FakeCurlSession
    requests_stub.get = _forbidden_new_connection
    package_stub = type(sys)("curl_cffi")
    package_stub.requests = requests_stub
    old = sys.modules.get("curl_cffi", _MISSING)
    sys.modules["curl_cffi"] = package_stub
    try:
        yield
    finally:
        if old is _MISSING:
            del sys.modules["curl_cffi"]
        else:
            sys.modules["curl_cffi"] = old


def test_two_fetches_reuse_one_curl_session():
    _FakeCurlSession.made = []
    _FakeCurlSession.blocked_for = ()

    with _patched(amazon, _sessions={}), _stub_curl_cffi():
        first = amazon._fetch_curl_cffi_page("q1", 1)
        second = amazon._fetch_curl_cffi_page("q2", 1)

    assert first and second
    assert len(_FakeCurlSession.made) == 1  # one session, not one connection per fetch
    assert len(_FakeCurlSession.made[0].gets) == 2


def test_a_blocked_fingerprint_falls_through_to_the_next_session():
    _FakeCurlSession.made = []
    _FakeCurlSession.blocked_for = ("chrome150",)
    try:
        with _patched(amazon, _sessions={}), _stub_curl_cffi():
            first = amazon._fetch_curl_cffi_page("q1", 1)
            second = amazon._fetch_curl_cffi_page("q2", 1)
    finally:
        _FakeCurlSession.blocked_for = ()

    assert first and second
    assert [s.impersonate for s in _FakeCurlSession.made] == ["chrome150", "chrome146"]
    assert len(_FakeCurlSession.made[1].gets) == 2  # chrome146 served both fetches


def test_resolve_chromedriver_asks_selenium_manager_once():
    import selenium.webdriver.common.selenium_manager as selenium_manager

    seen = {}

    def fake_binary_paths(self, args):
        seen["args"] = list(args)
        return {"browser_path": "C:/fake/chrome.exe", "driver_path": "C:/fake/chromedriver.exe"}

    with _patched(selenium_manager.SeleniumManager, binary_paths=fake_binary_paths):
        got = _base.resolve_chromedriver()

    assert got == "C:/fake/chromedriver.exe"
    assert seen["args"] == ["--browser", "chrome"]


def test_undetected_chromedriver_is_not_a_dependency():
    # The uc path is gone (Chrome 151 headless dies in it); nothing may
    # silently bring it back as a dependency.
    requirements = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(encoding="utf-8")
    active = [
        line for line in requirements.splitlines()
        if line.strip().startswith("undetected-chromedriver")
    ]
    assert not active


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
