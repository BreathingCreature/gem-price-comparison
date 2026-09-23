"""
Part 4 — Amazon.in scraper.

Primary path (2026-09 research-backed): curl_cffi TLS/JA3 browser
impersonation — no browser at all. This is the approach used by amzpy
(GitHub, MIT) and recommended across r/webscraping: Amazon's WAF fingerprints
the TLS handshake (JA3), so plain requests/urllib look like bots instantly,
while curl_cffi impersonating a real Chrome handshake gets full server-rendered
search HTML in ~2s. Live-verified on this machine: chrome150 fingerprint →
HTTP 200, 16 result cards, real prices, zero CAPTCHA.

Fallback path: headless Chrome via scrapers/base.build_stealth_driver
(undetected-chromedriver first, plain Selenium second). Used only when
curl_cffi is unavailable, errors, or Amazon serves it a wall/empty page.
Amazon serves soft bot-walls that look like ordinary pages, so this module
treats "page loaded but zero result containers and no explicit no-results
message" as a BLOCKED/suspect page (with a debug dump), not as "product
genuinely not sold here" — reporting a scrape failure as "not found" was a
real bug in the first version.

Selectors (data-component-type="s-search-result", h2 title, a-price
a-offscreen) are the long-stable documented Amazon search-card selectors.
Price parsing goes through base.parse_indian_price so "₹1,199.00" stays
"1199.00".
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from config import logger
from scrapers.base import CandidateResult, SearchScrapeResult, build_stealth_driver, detect_block, parse_indian_price
from scrapers.flipkart import DEBUG_DIR  # reuse the same debug-html folder

_BLOCK_PHRASES = [
    "type the characters you see",
    "enter the characters you see below",
    "api-services-support@amazon.com",
    "sorry, we just need to make sure you're not a robot",
    "automated access to amazon data",
    "enter the characters you see in this image",
    "tap the poles to continue",  # Amazon's image-select challenge wording
]

# Phrases that mean "genuinely zero results" — absence of cards is real here,
# not a bot wall.
_NO_RESULTS_PHRASES = [
    "no results for",
    "no results matching",
    "didn't match any products",
]


def _load_search_page(driver, query: str, page: int = 1) -> str | None:
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    url = f"https://www.amazon.in/s?k={quote_plus(query)}&page={page}"
    try:
        driver.get(url)
    except Exception as e:
        # Amazon is SLOW from here (renderer regularly exceeds the page-load
        # timeout) but keeps loading in the background — live-verified: the
        # "timed out" page_source eventually contained 16 real result cards.
        # Previously this returned None and threw the loaded page away.
        logger.warning(
            "amazon navigation slow on %r p%d (%s: %s) — waiting for the page to finish, keeping page_source.",
            query, page, type(e).__name__, str(e)[:120],
        )
        time.sleep(random.uniform(4.0, 6.0))

    # Amazon renders the search shell first and fills result cards via JS a
    # few seconds later — reading page_source too early gave a real page with
    # zero containers (mistaken for a bot wall). Wait for cards OR an
    # explicit no-results message before parsing.
    try:
        WebDriverWait(driver, 25).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, "div[data-component-type='s-search-result']")) > 0
            or _genuine_no_results(d.page_source)
        )
    except TimeoutException:
        logger.info("amazon results for %r p%d not rendered after wait — parsing whatever is present.", query, page)

    time.sleep(random.uniform(1.5, 2.5))  # let prices/titles settle
    if detect_block(driver, _BLOCK_PHRASES):
        return None
    return driver.page_source


def parse_cards_from_html(html: str) -> list[dict]:
    """Pure function, testable without a browser. Amazon search cards:
    container div[data-component-type='s-search-result'], title in an h2,
    price in span.a-price > span.a-offscreen (the visually-hidden exact
    price text — more reliable than the split whole/fraction spans)."""
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("div[data-component-type='s-search-result']")

    out = []
    for card in cards:
        # sponsored/placeholder cards sometimes have no asin — skip those
        if not card.get("data-asin"):
            continue

        title_el = card.select_one("h2 span.a-text-normal, h2 a span, h2 span")
        name = title_el.get_text(" ", strip=True) if title_el else ""

        price_el = card.select_one("span.a-price > span.a-offscreen")
        if price_el is None:
            price_el = card.select_one("span.a-price")
        price = parse_indian_price(price_el.get_text(" ", strip=True)) if price_el else None

        link_el = card.select_one("h2 a") or card.select_one("a.a-link-normal.s-no-outline")
        link = urljoin("https://www.amazon.in", link_el["href"]) if link_el and link_el.get("href") else None

        if not name or not price or not link:
            continue
        out.append({"name": name, "price": price, "link": link, "card_text": card.get_text(" ", strip=True)[:1000]})

    seen, uniq = set(), []
    for r in out:
        key = (r["name"], r["link"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def _genuine_no_results(html: str) -> bool:
    low = html.lower()
    return any(p in low for p in _NO_RESULTS_PHRASES)


def _save_debug(html: str, prefix: str) -> None:
    debug_path = DEBUG_DIR / f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.html"
    try:
        debug_path.write_text(html, encoding="utf-8")
        logger.warning("Saved suspect Amazon page to %s", debug_path)
    except Exception:
        pass


def _collect_cards(cards: list[dict], candidates: list[CandidateResult], seen: set, max_results: int) -> None:
    for c in cards:
        key = (c["name"], c["link"])
        if key in seen or len(candidates) >= max_results:
            continue
        seen.add(key)
        candidates.append(
            CandidateResult(
                source_domain="amazon.in",
                url=c["link"],
                scraper_used="amazon",
                structured_fields={
                    "title": c["name"],
                    "price": float(c["price"]) if c["price"] else None,
                    "currency": "INR",
                    "seller": None,
                    "image_url": None,
                    "in_stock": True,
                },
                raw_page_text=c["card_text"],
            )
        )


# curl_cffi fingerprints, newest first (see `curl-cffi list`). Verified live:
# chrome150 → HTTP 200 + 16 real cards on amazon.in from this machine.
_CURL_IMPERSONATE = ("chrome150", "chrome146", "chrome142", "safari180")


def _fetch_curl_cffi_page(query: str, page: int = 1) -> str | None:
    """Fetch one amazon.in SERP via TLS impersonation. Returns html, or None
    if curl_cffi is missing, errored, or Amazon served a bot-wall."""
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        logger.info("curl_cffi not installed — skipping Amazon fast path.")
        return None

    url = f"https://www.amazon.in/s?k={quote_plus(query)}&page={page}"
    headers = {"Accept-Language": "en-IN,en;q=0.9"}
    last_err: Exception | None = None

    for fp in _CURL_IMPERSONATE:
        try:
            r = curl_requests.get(url, impersonate=fp, timeout=30, headers=headers, allow_redirects=True)
        except Exception as e:
            last_err = e
            logger.warning("curl_cffi %s failed for %r: %s: %s", fp, query, type(e).__name__, str(e)[:160])
            continue

        html = r.text
        low = html.lower()
        blocked_hits = [p for p in _BLOCK_PHRASES if p in low]
        if r.status_code != 200 or blocked_hits:
            logger.warning(
                "curl_cffi %s: status=%s blocked=%s for %r — trying next fingerprint.",
                fp, r.status_code, blocked_hits or None, query,
            )
            _save_debug(html, "amazon_curl_blocked")
            continue
        return html

    if last_err is not None:
        logger.warning("curl_cffi exhausted all fingerprints for %r (last: %s)", query, last_err)
    return None


def _search_curl_cffi(query: str, max_results: int, max_pages: int) -> SearchScrapeResult | None:
    """Browser-free search. Returns a finished result, or None to tell the
    caller 'fast path inconclusive → fall back to the browser stack'.

    None is returned for: curl_cffi missing, network errors, bot-walls, and
    unparseable pages. A genuine 'Amazon has zero results for this query'
    page IS a finished result (empty candidates, blocked=False)."""
    candidates: list[CandidateResult] = []
    seen: set = set()
    pages_scraped = 0

    for page in range(1, max_pages + 1):
        if len(candidates) >= max_results:
            break

        html = _fetch_curl_cffi_page(query, page)
        if html is None:
            if pages_scraped == 0:
                return None  # never got a usable page → caller tries the browser
            break  # page 1 was fine, a later page failed → keep what we have

        cards = parse_cards_from_html(html)
        pages_scraped += 1

        if not cards:
            if _genuine_no_results(html):
                logger.info("Amazon (curl_cffi) reports no results for %r page %d.", query, page)
            elif pages_scraped == 1:
                _save_debug(html, "amazon_curl_suspect")
                logger.warning(
                    "Amazon (curl_cffi) page 1 for %r: zero cards and no no-results marker — "
                    "inconclusive, falling back to browser.",
                    query,
                )
                return None
            break

        _collect_cards(cards, candidates, seen, max_results)
        if page < max_pages:
            time.sleep(random.uniform(1.2, 2.5))  # polite: don't hammer Amazon

    if pages_scraped == 0:
        return None
    logger.info("Amazon (curl_cffi) got %d candidates from %d page(s) for %r.", len(candidates), pages_scraped, query)
    return SearchScrapeResult(
        source_domain="amazon.in",
        query=query,
        candidates=candidates,
        blocked=False,
        pages_scraped=pages_scraped,
        error=None,
    )


def search(query: str, max_results: int = 15, max_pages: int = 2) -> SearchScrapeResult:
    # Fast path: TLS impersonation, no browser (~2s vs ~60s, no window).
    fast = _search_curl_cffi(query, max_results, max_pages)
    if fast is not None:
        return fast
    logger.info("Amazon fast path inconclusive for %r — falling back to browser stack.", query)

    driver = build_stealth_driver()
    candidates: list[CandidateResult] = []
    seen: set[tuple] = set()
    blocked = False
    pages_scraped = 0
    error: str | None = None

    try:
        for page in range(1, max_pages + 1):
            if len(candidates) >= max_results:
                break
            html = _load_search_page(driver, query, page)
            if html is None:
                blocked = True
                _save_debug(driver.page_source, "amazon_block")
                logger.warning("Amazon blocked/CAPTCHA on %r page %d — stopping, no bypass.", query, page)
                break

            cards = parse_cards_from_html(html)
            pages_scraped += 1
            if not cards:
                if _genuine_no_results(html):
                    logger.info("Amazon page %d for %r: Amazon reports no results for this query.", page, query)
                else:
                    # Loaded a page with zero parseable cards and no
                    # no-results message = soft bot-wall or layout change.
                    # Call it blocked (not "not found") and dump for debugging.
                    blocked = True
                    _save_debug(html, "amazon_suspect")
                    logger.warning(
                        "Amazon page %d for %r: zero result cards and no no-results marker "
                        "(soft bot-wall or layout change) — marking blocked, not 'not found'.",
                        page,
                        query,
                    )
                break

            _collect_cards(cards, candidates, seen, max_results)
            time.sleep(random.uniform(1.5, 2.5))
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        logger.error("Amazon search failed for %r: %s", query, error)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return SearchScrapeResult(
        source_domain="amazon.in",
        query=query,
        candidates=candidates,
        blocked=blocked,
        pages_scraped=pages_scraped,
        error=error,
    )


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m scrapers.amazon <query>")
        sys.exit(1)

    result = search(sys.argv[1])
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
