"""
Part 4 — Amazon.in scraper.

UNVERIFIED — unlike flipkart.py, there was no prior tested code for Amazon
in the old repo. This follows the same pattern (headless Chrome via
base.build_stealth_driver(), search results page, parse cards) on the
assumption Amazon blocks plain requests the same way Flipkart does — that
assumption is untested here specifically. The selectors below
(data-component-type="s-search-result", a-price a-offscreen, etc.) are
widely-documented stable Amazon search-result selectors, not ones proven
against a live run from this project. First thing to check if this comes
back empty: run it once with a visible (non-headless) driver locally and
look at what actually loaded.
"""
from __future__ import annotations

import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from config import logger
from scrapers.base import CandidateResult, SearchScrapeResult, build_stealth_driver, detect_block
from scrapers.flipkart import DEBUG_DIR  # reuse the same debug-html folder

_BLOCK_PHRASES = [
    "type the characters you see",
    "enter the characters you see below",
    "api-services-support@amazon.com",
]

_PRICE_RE = re.compile(r"[\d,]+(?:\.\d+)?")


def _load_search_page(driver, query: str, page: int = 1) -> str | None:
    url = f"https://www.amazon.in/s?k={quote_plus(query)}&page={page}"
    try:
        driver.get(url)
    except Exception as e:
        logger.warning("amazon navigation error on %r p%d: %s: %s", query, page, type(e).__name__, e)
        return None

    time.sleep(random.uniform(2.0, 3.0))
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
        price = None
        if price_el:
            m = _PRICE_RE.search(price_el.get_text(strip=True).replace(",", ""))
            price = m.group(0) if m else None

        link_el = card.select_one("h2 a")
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


def search(query: str, max_results: int = 15, max_pages: int = 2) -> SearchScrapeResult:
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
                debug_path = DEBUG_DIR / f"amazon_block_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.html"
                try:
                    debug_path.write_text(driver.page_source, encoding="utf-8")
                    logger.warning("Amazon blocked/CAPTCHA on %r page %d — stopping, no bypass. Saved %s", query, page, debug_path)
                except Exception:
                    logger.warning("Amazon blocked/CAPTCHA on %r page %d — stopping, no bypass.", query, page)
                break

            cards = parse_cards_from_html(html)
            pages_scraped += 1
            if not cards:
                logger.info("Amazon page %d for %r: no cards parsed (selectors may not match, or genuinely no results).", page, query)
                break

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
