"""tools/try_flipkart_fast.py: can Flipkart's search be fetched without a browser?

Checkpoint 18 (faster waits): Flipkart search is the slowest scrape (Selenium
plus scroll rounds). This tool tries curl_cffi TLS impersonation against the
search URL and reports which fingerprint — if any — returns real result
cards. `_make_get` is the seam these tests fake; nothing touches the network.
"""
from types import SimpleNamespace

from tools import try_flipkart_fast


FLIPKART_HTML_WITH_CARDS = """
<div class="nZIRY7">
  <a href="/logitech-m650-wireless-mouse/p/itm1a2b3c4d5e?pid=MOUXYZ123">
    <div class="RG5Slk">Logitech M650 Wireless Mouse (Graphite)</div>
    <div class="hZ3P6w DeU9vF">₹1,299</div>
  </a>
</div>
"""


def _get_returning(html, status=200):
    def fake_get(url, impersonate=None, timeout=None):
        return SimpleNamespace(status_code=status, text=html)

    return fake_get


def test_a_fingerprint_that_returns_cards_is_a_success(monkeypatch, capsys):
    monkeypatch.setattr(try_flipkart_fast, "_make_get", lambda: _get_returning(FLIPKART_HTML_WITH_CARDS))
    assert try_flipkart_fast.main(["logitech m650"]) == 0
    out = capsys.readouterr().out
    assert "1 cards" in out
    assert "without Chrome" in out


def test_a_failing_fingerprint_moves_on_to_the_next(monkeypatch, capsys):
    calls = []

    def flaky_get(url, impersonate=None, timeout=None):
        calls.append(impersonate)
        if len(calls) == 1:
            raise ConnectionError("TLS reset")
        return SimpleNamespace(status_code=200, text=FLIPKART_HTML_WITH_CARDS)

    monkeypatch.setattr(try_flipkart_fast, "_make_get", lambda: flaky_get)
    assert try_flipkart_fast.main(["logitech m650"]) == 0
    assert len(calls) >= 2


def test_pages_without_cards_keep_the_chrome_path(monkeypatch, capsys):
    monkeypatch.setattr(try_flipkart_fast, "_make_get", lambda: _get_returning("<html>captcha</html>"))
    assert try_flipkart_fast.main(["logitech m650"]) == 1
    assert "keep using Chrome" in capsys.readouterr().out


def test_missing_curl_cffi_exits_with_2(monkeypatch, capsys):
    monkeypatch.setattr(try_flipkart_fast, "_make_get", lambda: None)
    assert try_flipkart_fast.main([]) == 2
    assert "curl_cffi" in capsys.readouterr().out
