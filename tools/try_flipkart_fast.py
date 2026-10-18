"""Try fetching Flipkart search results without a browser (checkpoint 18).

    python -m tools.try_flipkart_fast
    python -m tools.try_flipkart_fast "logitech m650 wireless mouse"

Flipkart search is the slowest scrape in the pipeline (Selenium + scroll
rounds). This tool tries curl_cffi TLS impersonation — the trick that made
Amazon's fast path work — against Flipkart's search URL and reports which
fingerprint, if any, returns real result cards. Nothing is changed here;
it only reports, so a failure costs a minute instead of a Chrome session.

Exit code: 0 when a fingerprint returned parseable cards, 1 when none did
(keep using Chrome), 2 when curl_cffi is not installed.
"""
from __future__ import annotations

import sys
from urllib.parse import quote_plus

from scrapers.flipkart import parse_cards_from_html

DEFAULT_QUERY = "logitech m650 wireless mouse"

# Same fingerprint rotation as amazon.py, newest first.
_CURL_IMPERSONATE = ("chrome150", "chrome146", "chrome142", "safari180")


def _make_get():
    """curl_cffi's get function, or None when curl_cffi is not installed."""
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        return None
    return curl_requests.get


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    query = argv[0] if argv else DEFAULT_QUERY
    url = f"https://www.flipkart.com/search?q={quote_plus(query)}"

    get = _make_get()
    if get is None:
        print("curl_cffi is not installed — pip install curl-cffi and try again.")
        return 2

    print(f"Trying {url} without a browser (curl_cffi TLS impersonation).")
    best = 0
    for fingerprint in _CURL_IMPERSONATE:
        try:
            resp = get(url, impersonate=fingerprint, timeout=30)
        except Exception as e:
            print(f"  {fingerprint:<10} failed  {type(e).__name__}: {str(e)[:120]}")
            continue
        html = getattr(resp, "text", "") or ""
        cards = parse_cards_from_html(html)
        note = " (captcha page)" if "recaptcha" in html.lower() else ""
        print(f"  {fingerprint:<10} HTTP {getattr(resp, 'status_code', '?')}  {len(cards)} cards{note}")
        best = max(best, len(cards))
        if cards:
            break

    print()
    if best:
        print(f"Result: the fast path looks possible — {best} cards parsed without Chrome.")
        print("Report this to the kit so a curl_cffi path with Chrome fallback can be built.")
        return 0
    print("Result: no fingerprint returned parseable cards — keep using Chrome for Flipkart.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
