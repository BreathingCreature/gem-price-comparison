"""
Part 4 — Flipkart scraper.

Searches Flipkart directly for a query and parses result cards — ported
from the old repo's flipkart_scraper.py, which was tested and documented
getting a hard 403+reCAPTCHA from plain requests, hence headless Chrome via
base.build_stealth_driver(). Adapted here to return CandidateResult /
SearchScrapeResult instead of writing straight to CSV, and to plug into
this project's contracts — the scraping logic itself (selectors, scroll
pattern, block detection) is carried over as-is.

Selectors WILL drift over time (Flipkart changes markup periodically) —
these were correct as of the old repo's last test, not guaranteed current.
If search() returns zero candidates with blocked=False, that's the first
thing to suspect: check the debug HTML this module saves on a zero-card
page against the selectors below and update them.
"""
from __future__ import annotations

import random
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from core.config import CACHE_DIR, FLIPKART_LIGHT, logger
from scrapers._base import CandidateResult, SearchScrapeResult, build_stealth_driver, detect_block, parse_indian_price

DEBUG_DIR = CACHE_DIR.parent / "debug_html"
DEBUG_DIR.mkdir(exist_ok=True)

PRICE_RE = re.compile(r"₹\s?([\d,]+)")

_BLOCK_PHRASES = ["flipkart recaptcha"]


def _scroll_to_load_cards(driver, *, max_rounds: int = 8, stop_when_over: int = 50) -> None:
    """Scroll to coax Flipkart's lazy-loaded result cards in. The old loop
    always ran all 8 rounds: its '>50 links' exit almost never triggers
    (result pages usually hold ~20-40 cards). Two rounds in a row that add
    no new product links mean the page is done — stop there."""
    from selenium.webdriver.common.by import By

    previous = -1
    stale_rounds = 0
    for _ in range(max_rounds):
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(random.uniform(1.0, 2.0))
            count = len(driver.find_elements(By.XPATH, "//a[contains(@href, '/p/')]"))
            if count > stop_when_over:
                break
            if count <= previous:
                stale_rounds += 1
                if stale_rounds >= 2:
                    break
            else:
                stale_rounds = 0
                previous = count
            driver.execute_script("window.scrollBy(0, -600)")
            time.sleep(random.uniform(0.5, 1.0))
        except Exception as e:
            logger.warning("flipkart scroll error: %s: %s", type(e).__name__, e)
            break


def _load_search_page(driver, query: str, page: int = 1) -> str | None:
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    url = f"https://www.flipkart.com/search?q={quote_plus(query)}&page={page}"
    try:
        driver.get(url)
    except Exception as e:
        # Same slow-page tolerance as amazon: a nav timeout doesn't mean the
        # page failed — wait for the background load and keep page_source.
        logger.warning(
            "flipkart navigation slow on %r p%d (%s: %s) — waiting for the page to finish, keeping page_source.",
            query, page, type(e).__name__, str(e)[:120],
        )
        time.sleep(random.uniform(4.0, 6.0))

    # Page 2+ especially renders its shell first and fills cards via JS —
    # reading too early gave "no cards parsed" on live runs. Wait for a
    # reasonable number of product links before the scroll pass.
    try:
        WebDriverWait(driver, 15).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, "div[data-id], a[href*='/p/']")) >= 5
        )
    except TimeoutException:
        logger.info("flipkart cards for %r p%d not rendered after wait — parsing whatever is present.", query, page)

    time.sleep(random.uniform(2.0, 3.0))  # initial render
    if detect_block(driver, _BLOCK_PHRASES):
        return None

    _scroll_to_load_cards(driver)
    return driver.page_source


def parse_cards_from_html(html: str) -> list[dict]:
    """Pure function, no browser — this is what's actually testable without
    Selenium/Chrome installed. Current Flipkart layout: card container
    div.nZIRY7, title div.RG5Slk, price div.hZ3P6w.DeU9vF, with older-layout
    fallbacks kept alongside."""
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("div.nZIRY7")
    if not cards:
        cards = soup.select("div[data-id]")
    if not cards:
        cards = soup.select('a[href*="/p/"]')

    out = []
    for card in cards:
        name_el = card.select_one("div.RG5Slk, div._4rR01T, div.KzDlHZ, div.syl9yP")
        if name_el:
            name = name_el.get_text(" ", strip=True)
        else:
            link_a = card.select_one('a[href*="/p/"]')
            name = (link_a.get_text(" ", strip=True) if link_a else "") or card.get_text(" ", strip=True)
            name = name.split("₹")[0].strip()[:200]
            name = name.replace("Add to Compare", "").strip()

        price = None
        price_el = card.select_one("div.hZ3P6w.DeU9vF, div.hZ3P6w, div.Nx9bqj, div._30jeq3")
        if price_el:
            # parse_indian_price, not digits-only: "₹1,499.00" via re.sub(r"[^\d]")
            # became "149900" (100x inflation) and ranges concatenated both ends.
            price = parse_indian_price(price_el.get_text(" ", strip=True))
        if not price:
            # Strip EMI lines first so "₹99/month" or "No Cost EMI from ₹99/month"
            # don't get picked as the product price.
            card_text = card.get_text(" ", strip=True)
            card_text = re.sub(r"₹\s*\d[\d,]*(?:\.\d+)?\s*/\s*month", "", card_text)
            card_text = re.sub(r"₹\s*\d[\d,]*(?:\.\d+)?\s*per\s*month", "", card_text, flags=re.I)
            card_text = re.sub(r"No\s*Cost\s*EMI\s*from\s*₹\s*\d[\d,]*(?:\.\d+)?", "", card_text, flags=re.I)
            card_text = re.sub(r"EMI", "", card_text, flags=re.I)
            price = parse_indian_price(card_text)

        link_el = card.select_one('a[href*="/p/"]')
        if link_el is None and getattr(card, "name", None) == "a" and "/p/" in (card.get("href") or ""):
            link_el = card
        # Keep the full href including ?pid=... — stripping the query collapsed
        # distinct variants onto one URL whose price belonged to another variant.
        link = (
            urljoin("https://www.flipkart.com", link_el["href"])
            if link_el and link_el.get("href")
            else None
        )

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


def _search_with_driver(driver, query: str, *, max_results: int = 15, max_pages: int = 3) -> SearchScrapeResult:
    """One search on an already-open driver — the whole search() body. Split
    out so a run can reuse one Chrome (FlipkartBrowser) instead of starting
    a new one per query."""
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
                debug_path = DEBUG_DIR / f"flipkart_block_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.html"
                try:
                    debug_path.write_text(driver.page_source, encoding="utf-8")
                    logger.warning("Flipkart blocked/CAPTCHA on %r page %d — stopping, no bypass. Saved %s", query, page, debug_path)
                except Exception:
                    logger.warning("Flipkart blocked/CAPTCHA on %r page %d — stopping, no bypass.", query, page)
                break

            cards = parse_cards_from_html(html)

            # Lazy-load miss on page 2+ (seen live): one reload + re-wait
            # before giving up. Page-1 results are already in hand, so a
            # second failure just stops pagination as before.
            if not cards and page >= 2:
                logger.info("Flipkart page %d for %r: zero cards — reloading once (lazy-load miss).", page, query)
                time.sleep(random.uniform(1.5, 2.5))
                html = _load_search_page(driver, query, page)
                if html is None:
                    # Reload hit CAPTCHA — previously fell through as
                    # "no cards parsed" with blocked=False, reporting a
                    # scrape failure as a clean empty search.
                    blocked = True
                    debug_path = DEBUG_DIR / f"flipkart_block_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.html"
                    try:
                        debug_path.write_text(driver.page_source, encoding="utf-8")
                        logger.warning("Flipkart blocked/CAPTCHA on %r page %d reload — stopping, no bypass. Saved %s", query, page, debug_path)
                    except Exception:
                        logger.warning("Flipkart blocked/CAPTCHA on %r page %d reload — stopping, no bypass.", query, page)
                    break
                cards = parse_cards_from_html(html)

            pages_scraped += 1
            if not cards:
                logger.info("Flipkart page %d for %r: no cards parsed (selectors may have drifted).", page, query)
                break

            for c in cards:
                key = (c["name"], c["link"])
                if key in seen or len(candidates) >= max_results:
                    continue
                seen.add(key)
                candidates.append(
                    CandidateResult(
                        source_domain="flipkart.com",
                        url=c["link"],
                        scraper_used="flipkart",
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
            # The pause between pages only exists to be polite to Flipkart —
            # no point taking it after the last page (or once we have enough).
            if page < max_pages and len(candidates) < max_results:
                time.sleep(random.uniform(1.0, 2.0))
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        logger.error("Flipkart search failed for %r: %s", query, error)

    return SearchScrapeResult(
        source_domain="flipkart.com",
        query=query,
        candidates=candidates,
        blocked=blocked,
        pages_scraped=pages_scraped,
        error=error,
    )


def search(query: str, max_results: int = 15, max_pages: int = 3) -> SearchScrapeResult:
    """One-off search: builds its own Chrome and quits it afterwards (the
    tools and older callers use this). A whole run uses FlipkartBrowser
    instead, so one Chrome serves every query and the retry."""
    driver = build_stealth_driver(light=FLIPKART_LIGHT)
    try:
        return _search_with_driver(driver, query, max_results=max_results, max_pages=max_pages)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _build_default_driver():
    return build_stealth_driver(light=FLIPKART_LIGHT)


class FlipkartBrowser:
    """ONE Chrome for a whole run (Flipkart Tier 2, items 1 and 2).

    start() begins building the driver in a background thread right away, so
    the 3-8 s startup hides behind the GeM page fetch and the LLM normalize
    instead of delaying the first search. Every search() after that reuses
    the same driver — all normalized queries and the retry included —
    instead of starting (and quitting) a new Chrome per query. close() quits
    it exactly once, even when the run ends early while the build is still
    running (a failed extract must not leak a Chrome process)."""

    def __init__(self, build_driver=None):
        self._build_driver = build_driver or _build_default_driver
        self._driver = None
        self._start_error: Exception | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

    def start(self) -> None:
        """Build the driver in the background. Calling it twice is harmless."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._build, name="flipkart-browser", daemon=True)
        self._thread.start()

    def _build(self) -> None:
        try:
            self._driver = self._build_driver()
        except Exception as e:
            self._start_error = e
            logger.warning("Flipkart Chrome could not start: %s: %s", type(e).__name__, e)
        finally:
            self._ready.set()

    def search(self, query: str, max_results: int = 15, max_pages: int = 3) -> SearchScrapeResult:
        self.start()
        self._ready.wait()
        if self._start_error is not None:
            raise self._start_error
        return _search_with_driver(self._driver, query, max_results=max_results, max_pages=max_pages)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            self._ready.wait()  # a half-built Chrome must not leak
        driver = self._driver
        if driver is None:
            return
        try:
            driver.quit()
        except Exception:
            logger.warning("Flipkart Chrome did not close cleanly.", exc_info=True)


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m scrapers.flipkart <query>")
        sys.exit(1)

    result = search(sys.argv[1])
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
