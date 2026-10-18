"""
Tests for discovery.py.

Same approach as every other part: pure-function parsing logic tested
against hand-built mock HTML via a fake requests session, no real network
call. This proves discover_candidates()'s filtering/dedup/block-detection
logic is correct. It does NOT prove Google's current results page still
matches either of the two link shapes handled below — that's flagged as
unverified in discovery.py's own docstring, and can only be confirmed by
an actual run outside this sandbox.
"""
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discovery  # noqa: E402
from discovery import discover_candidates, discover_for_queries  # noqa: E402

discovery.QUERY_DELAY_RANGE = (0.0, 0.0)  # no reason to actually wait in tests


class _FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, text: str):
        self._text = text

    def get(self, url, headers=None, timeout=None):
        return _FakeResponse(self._text)


# --- mock Google result pages -----------------------------------------------------

# Old-style /url?q= wrapped links
MOCK_HTML_URL_Q_STYLE = """
<html><body>
<a href="/url?q=https://www.croma.com/logitech-m650-mouse/p/12345&amp;sa=U">
  <h3>Logitech M650 Wireless Mouse - Croma</h3>
</a>
<a href="/url?q=https://en.wikipedia.org/wiki/Computer_mouse&amp;sa=U">
  <h3>Computer mouse - Wikipedia</h3>
</a>
</body></html>
"""

# Modern-style direct <a href> wrapping an <h3>
MOCK_HTML_H3_STYLE = """
<html><body>
<a href="https://www.reliancedigital.in/logitech-m650">
  <h3>Logitech M650 Wireless Mouse | Reliance Digital</h3>
</a>
<a href="https://www.google.com/search?q=related">
  <h3>Related searches</h3>
</a>
</body></html>
"""

MOCK_HTML_BLOCKED = """
<html><body>
Our systems have detected unusual traffic from your computer network.
</body></html>
"""


# --- _parse_result_urls -----------------------------------------------------------

def test_parse_result_urls_handles_url_q_style():
    urls = discovery._parse_result_urls(MOCK_HTML_URL_Q_STYLE)
    assert "https://www.croma.com/logitech-m650-mouse/p/12345" in urls
    assert "https://en.wikipedia.org/wiki/Computer_mouse" in urls


def test_parse_result_urls_handles_h3_style():
    urls = discovery._parse_result_urls(MOCK_HTML_H3_STYLE)
    assert "https://www.reliancedigital.in/logitech-m650" in urls


def test_parse_result_urls_handles_searxng_h3_wraps_a():
    # SearXNG shape: <h3><a href=...>title</a></h3> (opposite of Google's
    # <a><h3>). Live bug 2026-09-24: parser used find_parent("a") only and
    # extracted ZERO links from working SearXNG instances.
    html = """
    <html><body>
    <article class="result result-default">
      <a class="url_header" href="https://www.croma.com/product/p/1"><div class="url_wrapper">croma.com</div></a>
      <h3><a href="https://www.croma.com/product/p/1">Croma <span class="highlight">Mouse</span></a></h3>
    </article>
    <article class="result result-default">
      <h3><a href="https://www.snapdeal.com/product/x">Snapdeal Mouse</a></h3>
    </article>
    </body></html>
    """
    urls = discovery._parse_result_urls(html)
    assert "https://www.croma.com/product/p/1" in urls
    assert "https://www.snapdeal.com/product/x" in urls


# --- discover_candidates: filtering ------------------------------------------------

def test_discover_candidates_strict_mode_keeps_only_allowed_domains():
    session = _FakeSession(MOCK_HTML_URL_Q_STYLE)
    result = discovery.discover_candidates("logitech m650", ["croma.com"], session=session)
    assert result.filtered_urls == ["https://www.croma.com/logitech-m650-mouse/p/12345"]
    # wikipedia was in raw_urls but not in the allowed list
    assert any("wikipedia" in u for u in result.raw_urls)
    assert not any("wikipedia" in u for u in result.filtered_urls)


def test_discover_candidates_open_mode_excludes_non_retail_blocklist():
    session = _FakeSession(MOCK_HTML_URL_Q_STYLE)
    result = discovery.discover_candidates("logitech m650", None, session=session)
    # croma.com survives open discovery, wikipedia does not (soft blocklist)
    assert any("croma.com" in u for u in result.filtered_urls)
    assert not any("wikipedia" in u for u in result.filtered_urls)


def test_discover_candidates_open_mode_is_indian_sites_only():
    # User directive 2026-09-24: open discovery = Indian retail only.
    # Foreign stores must be dropped even though they aren't blocklisted.
    html = """
    <html><body>
    <a href="https://www.bestbuy.com/product/logitech-m650"><h3>M650 - Best Buy</h3></a>
    <a href="https://www.newegg.com/logitech-m650"><h3>M650 - Newegg</h3></a>
    <a href="https://www.croma.com/logitech-m650"><h3>M650 - Croma</h3></a>
    <a href="https://www.indiamart.com/proddetail/logitech-m650"><h3>M650 - IndiaMART</h3></a>
    <a href="https://somebrandstore.in/logitech-m650"><h3>M650 - .in store</h3></a>
    </body></html>
    """
    result = discovery.discover_candidates("logitech m650", None, session=_FakeSession(html))
    kept = " ".join(result.filtered_urls)
    assert "croma.com" in kept          # Indian .com marketplace (allowlisted)
    assert "indiamart.com" in kept      # Indian .com marketplace (allowlisted)
    assert "somebrandstore.in" in kept  # .in TLD
    assert "bestbuy.com" not in kept    # US store — dropped
    assert "newegg.com" not in kept     # US store — dropped


def test_discover_candidates_always_excludes_google_domains():
    session = _FakeSession(MOCK_HTML_H3_STYLE)
    result = discovery.discover_candidates("logitech m650", ["reliancedigital.in", "google.com"], session=session)
    # even if google.com is (accidentally) in allowed_domains, the
    # always-exclude list should still drop it
    assert not any("google.com" in u for u in result.filtered_urls)
    assert any("reliancedigital.in" in u for u in result.filtered_urls)


def test_discover_candidates_strict_mode_skips_direct_scraper_domains():
    # E2: if the LLM suggests amazon.in / flipkart.com in strict mode, the
    # generic scraper would fetch them by hand (and get blocked). They have
    # their own direct scrapers — they must not surface as discovery URLs.
    html = """
    <html><body>
    <a href="/url?q=https://www.amazon.in/logitech-m650/p/AMZ1&amp;sa=U">
      <h3>Logitech M650 - Amazon.in</h3>
    </a>
    <a href="/url?q=https://www.croma.com/logitech-m650/p/12345&amp;sa=U">
      <h3>Logitech M650 - Croma</h3>
    </a>
    </body></html>
    """
    result = discovery.discover_candidates(
        "logitech m650", ["amazon.in", "croma.com"], session=_FakeSession(html)
    )
    assert result.filtered_urls == ["https://www.croma.com/logitech-m650/p/12345"]


# --- block / error handling --------------------------------------------------------

def test_discover_candidates_detects_block():
    session = _FakeSession(MOCK_HTML_BLOCKED)
    result = discovery.discover_candidates("logitech m650", ["croma.com"], session=session)
    assert result.blocked is True
    assert result.filtered_urls == []


def test_discover_candidates_handles_request_exception():
    class _BrokenSession:
        def get(self, url, headers=None, timeout=None):
            raise ConnectionError("network down")

    result = discovery.discover_candidates("logitech m650", ["croma.com"], session=_BrokenSession())
    assert result.error is not None
    assert "network down" in result.error
    assert result.filtered_urls == []


# --- discover_for_queries: multiple queries -----------------------------------------

def test_discover_for_queries_returns_one_result_per_query():
    session = _FakeSession(MOCK_HTML_URL_Q_STYLE)
    results = discovery.discover_for_queries(["query one", "query two"], ["croma.com"], session=session)
    assert len(results) == 2
    assert results[0].query == "query one"
    assert results[1].query == "query two"
    assert all(r.filtered_urls for r in results)


# --- SearXNG instances are asked at the same time, with a total time limit -----------


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


SEARXNG_MOCK_HTML = """
<html><body>
<article class="result result-default">
  <h3><a href="https://www.croma.com/logitech-m650">Logitech M650 - Croma</a></h3>
</article>
</body></html>
"""


class _DdgDownCountingSession:
    """DDG and Google are unreachable; SearXNG requests go through handler
    while the session counts how many are in flight at once."""

    def __init__(self, handler):
        self.handler = handler
        self.active = 0
        self.max_active = 0

    def get(self, url, headers=None, timeout=None):
        if "duckduckgo.com" in url or "google.com" in url:
            raise ConnectionError("backend down")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            return self.handler(url)
        finally:
            self.active -= 1


def test_searxng_instances_are_asked_at_the_same_time():
    # Live pain: up to 11 instances tried one by one, each allowed 15s —
    # one dead instance must not cost the others' wait.
    def slow_response(url):
        time.sleep(0.2)
        return _FakeResponse(SEARXNG_MOCK_HTML)

    session = _DdgDownCountingSession(slow_response)
    result = discovery.discover_candidates("logitech m650", ["croma.com"], session=session)
    assert result.filtered_urls == ["https://www.croma.com/logitech-m650"]
    assert session.max_active >= 2


def test_searxng_search_has_a_total_time_limit():
    # Even in parallel, the stage needs a deadline — otherwise the slowest
    # allowed attempt (15s) sets the floor for a run.
    def dead_slow_response(url):
        time.sleep(1.0)
        return _FakeResponse(SEARXNG_MOCK_HTML)

    session = _DdgDownCountingSession(dead_slow_response)
    # patch the real module — the package re-exports only a few names
    with _patched(discovery.discover, _SEARXNG_INSTANCES=("https://a.example", "https://b.example"),
                  DISCOVERY_TIME_LIMIT_SECONDS=0.3):
        started = time.monotonic()
        result = discovery.discover_candidates("logitech m650", ["croma.com"], session=session)
        elapsed = time.monotonic() - started
    assert elapsed < 0.9  # did not wait out the 1s fetches
    assert result.filtered_urls == []


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
