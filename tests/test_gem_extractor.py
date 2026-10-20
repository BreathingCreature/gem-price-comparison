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

import requests  # noqa: E402

from scrapers.gem import (  # noqa: E402
    classify_price,
    extract_gem_product,
    parse_gem_url,
    _extract_specs,
)
from scrapers import gem as gem_module  # noqa: E402
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


def test_classify_price_lone_comma_after_keyword_does_not_crash():
    # D1: the old digit pattern matched a lone comma, then float("") raised.
    assert classify_price("Our Price: , ₹2725") == (2725.0, "fixed")


def test_classify_price_lone_comma_without_keyword_does_not_crash():
    assert classify_price("Price , 2725") == (2725.0, "fixed")


def test_classify_price_spec_cases():
    assert classify_price("₹ 1,299") == (1299.0, "fixed")
    assert classify_price("L1 ₹1,299") == (1299.0, "L1_rate")


def test_classify_price_savings_text_is_not_a_range():
    # D2: several numbers but no range shape ("x% You Save: y") — first
    # number wins as "fixed", not "range".
    assert classify_price("Rs.2,725.00 17% You Save: ₹570.00") == (2725.0, "fixed")


def test_classify_price_range_shapes():
    assert classify_price("₹1,000 – ₹1,300") == (1000.0, "range")
    assert classify_price("₹1,000 — ₹1,300") == (1000.0, "range")
    assert classify_price("₹1,000 to ₹1,300") == (1000.0, "range")


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
    """Read-only .text, like a real requests.Response."""

    def __init__(self, text: str):
        self._text = text

    @property
    def text(self) -> str:
        return self._text

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


def test_extract_gem_product_empty_page_raises_clear_error():
    # D3: a block page, a captcha, or a changed layout must NOT reach the
    # pipeline as an empty product — raise a clear error instead.
    url = "https://mkp.gem.gov.in/some-cat/some-product/p-1-2-cat.html"
    fake_session = _FakeSession("<html><body>Nothing here</body></html>")

    raised = None
    try:
        extract_gem_product(url, session=fake_session)
    except RuntimeError as e:
        raised = e

    assert raised is not None, "no title + no price must raise RuntimeError"
    assert "no title and no price" in str(raised)
    assert url in str(raised)


MOCK_TITLE_ONLY_HTML = """
<html><body>
  <div class="variant-desc">
    <span class="variant-title">Logitech M650 Wireless Mouse</span>
  </div>
</body></html>
"""


def test_extract_gem_product_title_without_price_still_works():
    # D3: a page with a title but no price still extracts (price gates stay
    # off) — only the title+price double-miss is fatal.
    url = "https://mkp.gem.gov.in/computer-mouse/logitech-m650/p-5116877-2-cat.html"
    fake_session = _FakeSession(MOCK_TITLE_ONLY_HTML)

    product = extract_gem_product(url, session=fake_session)

    assert "M650" in product.title
    assert product.gem_price is None
    assert product.gem_price_type == "unknown"


# --- retry on flaky GeM fetches (D4) --------------------------------------------

class _ScriptedResponse:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self._text = text

    @property
    def text(self) -> str:
        return self._text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)


class _ScriptedSession:
    """get() returns the mock HTML; queued steps are exceptions to raise or
    int HTTP statuses to serve once before the good response."""

    def __init__(self, steps, html: str = MOCK_PRODUCT_HTML):
        self._steps = list(steps)
        self._html = html
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        if self._steps:
            step = self._steps.pop(0)
            if isinstance(step, Exception):
                raise step
            if isinstance(step, int):
                _ScriptedResponse(step).raise_for_status()
        return _ScriptedResponse(200, self._html)


from contextlib import contextmanager  # noqa: E402


@contextmanager
def _no_retry_sleep():
    """Make gem's retry sleep instant and record how many sleeps happened."""
    calls = []
    old_sleep = gem_module.time.sleep
    old_secs = gem_module.RETRY_SLEEP_SECONDS

    def _fake_sleep(seconds):
        calls.append(seconds)

    gem_module.time.sleep = _fake_sleep
    gem_module.RETRY_SLEEP_SECONDS = 0.0
    try:
        yield calls
    finally:
        gem_module.time.sleep = old_sleep
        gem_module.RETRY_SLEEP_SECONDS = old_secs


_RETRY_TEST_URL = "https://mkp.gem.gov.in/computer-mouse/logitech-m650/p-5116877-2-cat.html"


def test_extract_gem_product_retries_once_after_timeout():
    # D4: first call times out, second succeeds -> product comes back.
    session = _ScriptedSession([requests.exceptions.Timeout("timed out")])
    with _no_retry_sleep() as sleeps:
        product = extract_gem_product(_RETRY_TEST_URL, session=session)
    assert "M650" in product.title
    assert session.calls == 2
    assert len(sleeps) == 1


def test_extract_gem_product_404_does_not_retry():
    # D4: 404 is permanent — raise after exactly one call.
    session = _ScriptedSession([404])
    with _no_retry_sleep() as sleeps:
        raised = None
        try:
            extract_gem_product(_RETRY_TEST_URL, session=session)
        except requests.exceptions.HTTPError:
            raised = True
    assert raised is True
    assert session.calls == 1
    assert sleeps == []


def test_extract_gem_product_503_retries_then_succeeds():
    session = _ScriptedSession([503])
    with _no_retry_sleep() as sleeps:
        product = extract_gem_product(_RETRY_TEST_URL, session=session)
    assert "M650" in product.title
    assert session.calls == 2
    assert len(sleeps) == 1


def test_extract_gem_product_two_timeouts_raise():
    # D4: two attempts total, then the error surfaces.
    session = _ScriptedSession([
        requests.exceptions.Timeout("t1"),
        requests.exceptions.Timeout("t2"),
    ])
    with _no_retry_sleep() as sleeps:
        raised = None
        try:
            extract_gem_product(_RETRY_TEST_URL, session=session)
        except requests.exceptions.Timeout:
            raised = True
    assert raised is True
    assert session.calls == 2
    assert len(sleeps) == 1


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
