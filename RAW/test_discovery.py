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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discovery  # noqa: E402

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


def test_discover_candidates_always_excludes_google_domains():
    session = _FakeSession(MOCK_HTML_H3_STYLE)
    result = discovery.discover_candidates("logitech m650", ["reliancedigital.in", "google.com"], session=session)
    # even if google.com is (accidentally) in allowed_domains, the
    # always-exclude list should still drop it
    assert not any("google.com" in u for u in result.filtered_urls)
    assert any("reliancedigital.in" in u for u in result.filtered_urls)


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
