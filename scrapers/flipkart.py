"""
Flipkart scraper.

Searches Flipkart directly for a query and parses result cards. Fast path
first: one plain HTTP GET of the search page (curl_cffi TLS impersonation,
no browser at all) when GEM_FLIPKART_LIGHT is on, which is the default. If
that page looks real it is used; otherwise the page is re-checked once, and
only then does headless Chrome via base.build_stealth_driver() take over.
The fast path exists because Chrome startup dominates a run's wall time.

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

from core import prefilter
from core.config import CACHE_DIR, FLIPKART_LIGHT, logger
from scrapers._base import CandidateResult, SearchScrapeResult, build_stealth_driver, detect_block, parse_indian_price

DEBUG_DIR = CACHE_DIR.parent / "debug_html"
try:
    DEBUG_DIR.mkdir(exist_ok=True)
except OSError:
    pass  # a read-only tree must not break the import; _save_debug retries


_BLOCK_PHRASES = ["flipkart recaptcha", "recaptcha", "please verify you are a human", "not a robot"]

# Flipkart says this when the query genuinely has no products.
_NO_RESULTS_PHRASES = ["no products found", "no results found", "didn't find any products"]

# MRP / struck-out price containers — never the selling price.
_MRP_CLASSES = ("div._3I9_wc", "div.yRaY8j", "div._3auQ3N")

_OUT_OF_STOCK_PHRASES = ["sold out", "out of stock", "currently unavailable"]


def _in_stock(card_text: str) -> bool:
    low = card_text.lower()
    return not any(phrase in low for phrase in _OUT_OF_STOCK_PHRASES)


def _visible_text(html: str) -> str:
    """The text a person would see: no scripts, styles or JSON blobs. A
    results-page shell can carry 'no results found' inside its own script
    data without ever showing it."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup.get_text(" ", strip=True).lower()


def _genuine_no_results(html: str) -> bool:
    # Cheap check first; only a page that mentions a phrase is parsed.
    low = html.lower()
    if not any(phrase in low for phrase in _NO_RESULTS_PHRASES):
        return False
    visible = _visible_text(html)
    return any(phrase in visible for phrase in _NO_RESULTS_PHRASES)


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


# EMI text on a card ("No Cost EMI from ₹99/month") is not the product price.
# Order matters: remove the EMI phrase WITH its /month amount first. An amount
# without /month or "per month" after it is never touched, so "EMI starts
# ₹1,299" keeps its 1299.
_AMOUNT = r"₹\s*\d[\d,]*(?:\.\d+)?"
_PER_MONTH = r"(?:/\s*(?:month|mo)\b|\s*per\s*month)"
_EMI_PHRASE = re.compile(r"(?:no\s*cost\s*)?emi\s*(?:from|starts?(?:\s*(?:at|from))?|@)?\s*" + _AMOUNT + r"\s*" + _PER_MONTH, re.I)
_MONTHLY_AMOUNT = re.compile(_AMOUNT + r"\s*" + _PER_MONTH, re.I)
_EMI_WORD = re.compile(r"\bemi\b", re.I)


def strip_emi_text(text: str) -> str:
    """Card text without EMI phrases and per-month amounts."""
    text = _EMI_PHRASE.sub(" ", text)
    text = _MONTHLY_AMOUNT.sub(" ", text)
    return _EMI_WORD.sub(" ", text)



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

        # Struck-out MRP containers are never the selling price — remove them
        # before ANY price read, so the fallback cannot grab the MRP just
        # because the selling-price class drifted to an unknown name.
        for mrp in card.select(", ".join(_MRP_CLASSES)):
            mrp.decompose()

        price = None
        price_el = card.select_one("div.hZ3P6w.DeU9vF, div.hZ3P6w, div.Nx9bqj, div._30jeq3")
        if price_el:
            # parse_indian_price, not digits-only: "₹1,499.00" via re.sub(r"[^\d]")
            # became "149900" (100x inflation) and ranges concatenated both ends.
            price = parse_indian_price(price_el.get_text(" ", strip=True))
        if not price:
            # Remove the EMI phrases first (whole phrase, amount included), so
            # "No Cost EMI from ₹99/month ₹1,299 ₹2,499" gives 1299, not 2499.
            card_text = strip_emi_text(card.get_text(" ", strip=True))
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
        card_text = card.get_text(" ", strip=True)[:1000]
        out.append({
            "name": name,
            "price": price,
            "link": link,
            "card_text": card_text,
            "in_stock": _in_stock(card_text),
        })

    seen, uniq = set(), []
    for r in out:
        key = (r["name"], r["link"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


_LIGHT_IMPERSONATE = "chrome150"
_LIGHT_TIMEOUT_SECONDS = 25


def _looks_like_wall(html: str) -> bool:
    low = html.lower()
    return any(phrase in low for phrase in _BLOCK_PHRASES)


def _fetch_light_page(query: str, page: int = 1) -> str | None:
    """One plain HTTP GET of a Flipkart search page (no browser).

    Returns None when curl_cffi is missing, the request errors, the status is
    not 200, or the page is a bot-wall — the caller then uses Chrome. One
    attempt only: the spec's cost model is a single cheap request, not a
    fingerprint tournament that can burn a minute before Chrome starts.
    """
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        logger.info("curl_cffi not installed — skipping the Flipkart fast path.")
        return None

    url = f"https://www.flipkart.com/search?q={quote_plus(query)}&page={page}"
    headers = {"Accept-Language": "en-IN,en;q=0.9"}
    try:
        response = curl_requests.get(
            url, impersonate=_LIGHT_IMPERSONATE, timeout=_LIGHT_TIMEOUT_SECONDS,
            headers=headers, allow_redirects=True,
        )
    except Exception as e:
        logger.warning(
            "Flipkart fast path failed for %r: %s: %s",
            query, type(e).__name__, str(e)[:160],
        )
        return None
    if response.status_code != 200:
        logger.info("Flipkart fast path got HTTP %s for %r.", response.status_code, query)
        return None
    html = response.text
    if _looks_like_wall(html):
        _save_debug(html, "flipkart_light_block")
        return None
    return html


def _light_page_is_usable(html: str) -> bool:
    """A fast-path page counts only when it clearly is a results page: the
    genuine no-results message, or at least three parsed cards. Anything
    else (login wall, captcha shell, layout flip) goes to Chrome."""
    if _genuine_no_results(html):
        return True
    return len(parse_cards_from_html(html)) >= 3


def _candidate_from_card(card: dict) -> CandidateResult:
    return CandidateResult(
        source_domain="flipkart.com",
        url=card["link"],
        scraper_used="flipkart",
        structured_fields={
            "title": card["name"],
            "price": float(card["price"]) if card["price"] else None,
            "currency": "INR",
            "seller": None,
            "image_url": None,
            "in_stock": bool(card.get("in_stock", True)),
        },
        raw_page_text=card["card_text"],
    )


def _search_light(
    query: str,
    *,
    max_results: int = 15,
    max_pages: int = 3,
    product: set[str] | None = None,
    fetch_page=None,
) -> SearchScrapeResult | None:
    """Browser-free Flipkart search. Returns a finished result, or None to
    say 'fast path inconclusive, use Chrome'."""
    fetch_page = fetch_page or _fetch_light_page
    candidates: list[CandidateResult] = []
    seen: set[tuple] = set()
    pages_scraped = 0
    relevant = 0

    for page in range(1, max_pages + 1):
        if len(candidates) >= max_results or (product is not None and relevant >= prefilter.ENOUGH_RELEVANT):
            break
        html = fetch_page(query, page)
        if html is None:
            if page == 1:
                return None  # page 1 decides: Chrome takes over
            break  # page 1 was good; keep what it gave us
        if page == 1 and not _light_page_is_usable(html):
            logger.info("Flipkart fast path for %r does not look real — using Chrome.", query)
            return None
        if page > 1 and not _light_page_is_usable(html) and not _genuine_no_results(html):
            break  # page 1 was fine; a later page being odd only stops paging
        cards = parse_cards_from_html(html)
        pages_scraped += 1
        if not cards:
            break
        for card in cards:
            key = (card["name"], card["link"])
            if key in seen or len(candidates) >= max_results:
                continue
            seen.add(key)
            candidates.append(_candidate_from_card(card))
            if product is not None and prefilter.relevant_title(card["name"], product):
                relevant += 1

    logger.info("Flipkart (fast path) got %d candidates from %d page(s) for %r.", len(candidates), pages_scraped, query)
    return SearchScrapeResult(
        source_domain="flipkart.com",
        query=query,
        candidates=candidates,
        blocked=False,
        pages_scraped=pages_scraped,
        error=None,
        status="ok",
        reason=None if candidates else "Flipkart reported no results",
    )


def _save_debug(html: str, prefix: str) -> None:
    path = DEBUG_DIR / f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.html"
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        logger.warning("Saved Flipkart page to %s", path)
    except Exception:
        pass


def _search_with_driver(driver, query: str, *, max_results: int = 15, max_pages: int = 3, product: set[str] | None = None) -> SearchScrapeResult:
    """One search on an already-open driver — the whole search() body. Split
    out so a run can reuse one Chrome (FlipkartBrowser) instead of starting
    a new one per query.

    product, when given, is the reference product's words (C6): the page loop
    stops early once page 1 holds enough relevant cards, instead of always
    chasing the 15-card target through pages 2 and 3."""
    candidates: list[CandidateResult] = []
    seen: set[tuple] = set()
    blocked = False
    parser_empty = False
    pages_scraped = 0
    error: str | None = None
    relevant = 0

    def _enough() -> bool:
        return len(candidates) >= max_results or (product is not None and relevant >= prefilter.ENOUGH_RELEVANT)

    try:
        for page in range(1, max_pages + 1):
            if _enough():
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
                if _genuine_no_results(html):
                    logger.info("Flipkart page %d for %r: Flipkart reports no results.", page, query)
                else:
                    parser_empty = True
                    logger.info("Flipkart page %d for %r: no cards parsed (selectors may have drifted).", page, query)
                break

            for c in cards:
                key = (c["name"], c["link"])
                if key in seen or len(candidates) >= max_results:
                    continue
                seen.add(key)
                candidates.append(_candidate_from_card(c))
                if product is not None and prefilter.relevant_title(c["name"], product):
                    relevant += 1
            # The pause between pages only exists to be polite to Flipkart —
            # no point taking it after the last page (or once we have enough).
            if page < max_pages and not _enough():
                time.sleep(random.uniform(1.0, 2.0))
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        logger.error("Flipkart search failed for %r: %s", query, error)

    if error:
        status, reason = None, None
    elif blocked:
        status, reason = "blocked", "captcha or robot check"
    elif parser_empty:
        status, reason = "parser_empty", "the page had no product cards"
        error = "parser_empty: the page had no product cards"
    else:
        status, reason = "ok", None if candidates else "Flipkart reported no results"

    return SearchScrapeResult(
        source_domain="flipkart.com",
        query=query,
        candidates=candidates,
        blocked=blocked,
        pages_scraped=pages_scraped,
        error=error,
        status=status,
        reason=reason,
    )


def search(query: str, max_results: int = 15, max_pages: int = 3, product: set[str] | None = None) -> SearchScrapeResult:
    """One-off search: plain HTTP first (GEM_FLIPKART_LIGHT), then its own
    Chrome which is quit afterwards (tools and older callers use this). A
    whole run uses FlipkartBrowser instead, so one Chrome serves every query
    and the retry."""
    if FLIPKART_LIGHT:
        light = _search_light(query, max_results=max_results, max_pages=max_pages, product=product)
        if light is not None:
            return light
    driver = build_stealth_driver(light=FLIPKART_LIGHT)
    try:
        return _search_with_driver(driver, query, max_results=max_results, max_pages=max_pages, product=product)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _build_default_driver():
    return build_stealth_driver(light=FLIPKART_LIGHT)


_DEAD_DRIVER_MARKERS = (
    "invalid session id",
    "session deleted",
    "no such window",
    "chrome not reachable",
    "disconnected",
    "target window already closed",
    "web view not found",
)


def _driver_died(exc: BaseException | None = None, text: str | None = None) -> bool:
    blob = f"{type(exc).__name__ if exc else ''} {exc or ''} {text or ''}".lower()
    return any(marker in blob for marker in _DEAD_DRIVER_MARKERS)


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
        self._rebuilt = False

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

    def search(self, query: str, max_results: int = 15, max_pages: int = 3, product: set[str] | None = None) -> SearchScrapeResult:
        # Fast path first: a plain HTTP page that looks real means Chrome is
        # never started for this run (GEM_FLIPKART_LIGHT).
        if FLIPKART_LIGHT:
            light = _search_light(query, max_results=max_results, max_pages=max_pages, product=product)
            if light is not None:
                return light

        self.start()
        self._ready.wait()
        if self._start_error is not None:
            raise self._start_error
        try:
            result = _search_with_driver(self._driver, query, max_results=max_results, max_pages=max_pages, product=product)
        except Exception as e:
            if self._rebuilt or not _driver_died(e):
                raise
            self._rebuild()
            return _search_with_driver(self._driver, query, max_results=max_results, max_pages=max_pages, product=product)
        if self._rebuilt or not _driver_died(text=result.error or ""):
            return result
        # The driver died under us (window closed, session gone). One rebuild
        # per run: a second death is a real failure, not a hiccup.
        self._rebuild()
        return _search_with_driver(self._driver, query, max_results=max_results, max_pages=max_pages, product=product)

    def _rebuild(self) -> None:
        self._rebuilt = True
        old = self._driver
        if old is not None:
            try:
                old.quit()
            except Exception:
                pass
        logger.warning("Flipkart Chrome died — rebuilding it once for this run.")
        self._driver = self._build_driver()

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
