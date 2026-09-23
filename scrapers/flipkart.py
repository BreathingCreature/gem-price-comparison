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
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from config import CACHE_DIR, logger
from scrapers.base import CandidateResult, SearchScrapeResult, build_stealth_driver, detect_block, parse_indian_price

DEBUG_DIR = CACHE_DIR.parent / "debug_html"
DEBUG_DIR.mkdir(exist_ok=True)

PRICE_RE = re.compile(r"₹\s?([\d,]+)")

_BLOCK_PHRASES = ["flipkart recaptcha"]


def _load_search_page(driver, query: str, page: int = 1) -> str | None:
    from selenium.webdriver.common.by import By

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

    time.sleep(random.uniform(2.0, 3.0))  # initial render
    if detect_block(driver, _BLOCK_PHRASES):
        return None

    for _ in range(8):
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(random.uniform(1.0, 2.0))
            if len(driver.find_elements(By.XPATH, "//a[contains(@href, '/p/')]")) > 50:
                break
            driver.execute_script("window.scrollBy(0, -600)")
            time.sleep(random.uniform(0.5, 1.0))
        except Exception as e:
            logger.warning("flipkart scroll error: %s: %s", type(e).__name__, e)
            break
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
            price = parse_indian_price(card.get_text(" ", strip=True))

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


def search(query: str, max_results: int = 15, max_pages: int = 3) -> SearchScrapeResult:
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
                debug_path = DEBUG_DIR / f"flipkart_block_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.html"
                try:
                    debug_path.write_text(driver.page_source, encoding="utf-8")
                    logger.warning("Flipkart blocked/CAPTCHA on %r page %d — stopping, no bypass. Saved %s", query, page, debug_path)
                except Exception:
                    logger.warning("Flipkart blocked/CAPTCHA on %r page %d — stopping, no bypass.", query, page)
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
            time.sleep(random.uniform(1.0, 2.0))
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        logger.error("Flipkart search failed for %r: %s", query, error)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return SearchScrapeResult(
        source_domain="flipkart.com",
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
        print("Usage: python -m scrapers.flipkart <query>")
        sys.exit(1)

    result = search(sys.argv[1])
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
