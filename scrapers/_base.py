"""
Part 4 — shared scraper contracts.

Two different shapes here, because Amazon/Flipkart and "everything else"
actually work differently in practice, not just in theory:

- Flipkart's scraper (ported from the old repo, tested there) never visits
  individual product pages — it drives headless Chrome to Flipkart's own
  search results page for a query and parses (name, price, link) straight
  off the result cards. Amazon follows the same pattern below, since
  there's no tested reason to do it differently and Amazon's search cards
  give the same kind of structured data.
- Everything else (brand sites, anything Part 6's retry step turns up)
  goes through generic.py's per-URL scrape(url) instead — there's no
  "search this site directly" shortcut for an arbitrary domain.

This means Part 3 (discovery) and Part 6 (orchestrator) need to treat
Flipkart/Amazon as "query them directly" sources, not "discover via Google,
then visit each URL" like everything else. Flagging that now since it
changes how those parts get wired, even though they're not built yet.
"""
from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass
from typing import Optional


def parse_indian_price(text: Optional[str]) -> Optional[str]:
    """Extract a marketplace price as a clean numeric STRING (tests and
    callers rely on string form; float() conversion happens upstream).

    Handles the forms Indian marketplace cards actually use:
      ₹1,299        -> "1299"
      ₹1,299.00     -> "1299.00"   (digits-only would give 129900 — the bug)
      ₹1,499-₹1,999 -> "1499"      (range: first amount)
      1,499.00      -> "1499.00"   (no rupee sign, but comma-grouped)

    The no-rupee fallback only accepts comma-grouped amounts, never a bare
    digit run: "Intel Core i3 1215U" used to parse as price "3" (spec digits
    read as money when the price selector drifted). A bare unformatted
    number with no ₹ anywhere in the card is not recognized — returning
    None (candidate dropped / falls back to full card text) beats returning
    a wrong price.
    """
    if not text:
        return None
    m = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", text) or re.search(r"\b(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?)\b", text)
    return m.group(1).replace(",", "") if m else None


@dataclass
class CandidateResult:
    source_domain: str
    url: str
    scraper_used: str  # "amazon" | "flipkart" | "generic"
    structured_fields: Optional[dict]  # title, price, currency, seller, image_url, in_stock
    raw_page_text: Optional[str]
    fetch_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "source_domain": self.source_domain,
            "url": self.url,
            "scraper_used": self.scraper_used,
            "structured_fields": self.structured_fields,
            "raw_page_text": self.raw_page_text,
            "fetch_error": self.fetch_error,
        }


@dataclass
class SearchScrapeResult:
    """Returned by amazon.search() / flipkart.search() — one call per query,
    covers however many result cards were found. Block/error status lives
    at this batch level because that's how these sites actually fail (the
    whole search gets blocked, not one card at a time) — this is what Part
    8's trace viewer will show per marketplace per run."""

    source_domain: str
    query: str
    candidates: list  # list[CandidateResult]
    blocked: bool = False
    pages_scraped: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "source_domain": self.source_domain,
            "query": self.query,
            "candidates": [c.to_dict() for c in self.candidates],
            "blocked": self.blocked,
            "pages_scraped": self.pages_scraped,
            "error": self.error,
        }


# --- shared stealth-browser setup ---------------------------------------------
# Ported near-verbatim from the old repo's build_driver() — proven against
# Flipkart's bot detection. Amazon uses the exact same setup; nothing here
# is site-specific, so it isn't duplicated per scraper.

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# undetected-chromedriver is OFF by default on this machine (2026-09):
# Chrome 151 + uc 3.5.5 headless sessions start then immediately die with
# NoSuchWindowException ("web view not found"), and uc's chromedriver
# download is flaky behind this network (SSL/EOF errors). Plain Selenium
# with the CDP webdriver-mask has been reliably serving Flipkart, and
# Amazon no longer needs a browser at all (curl_cffi path in amazon.py).
# Flip a local override if uc ever gets its act together:
#   $env:GEM_USE_UC = "1"
_USE_UC = False if os.getenv("GEM_USE_UC", "0") != "1" else None


def build_stealth_driver():
    """Headless Chrome with the automation tells masked. Not a CAPTCHA
    bypass — just stops the driver announcing itself as a bot before a
    CAPTCHA is even triggered. If a site still blocks it, detect_block()
    below is what notices, and the caller stops rather than fighting it.

    Primary path: undetected-chromedriver (free, the standard open-source
    answer to Amazon's soft bot-walls — patches the chromedriver binary so
    Chrome doesn't announce automation). Fallback: plain Selenium with CDP
    webdriver-mask, which is what always worked for Flipkart."""
    global _USE_UC
    if _USE_UC is not False:
        try:
            import undetected_chromedriver as uc

            def _uc_options():
                # Fresh object per attempt — uc consumes/mutates options, and
                # reusing one raises "you cannot reuse the ChromeOptions object".
                o = uc.ChromeOptions()
                o.add_argument("--headless=new")
                o.add_argument("--no-sandbox")
                o.add_argument("--no-first-run")
                o.add_argument("--no-default-browser-check")
                o.add_argument("--disable-gpu")
                o.add_argument("--window-size=1366,2000")
                o.add_argument(f"user-agent={UA}")
                return o

            try:
                driver = uc.Chrome(options=_uc_options(), use_subprocess=True)
            except Exception as first_exc:
                # uc's auto version detection misses on very new Chrome
                # ("Current browser version is 151.x" appears in the error) —
                # parse the major version and retry with it pinned.
                m = re.search(r"browser version is (\d+)", str(first_exc))
                if not m:
                    raise
                driver = uc.Chrome(options=_uc_options(), use_subprocess=True, version_main=int(m.group(1)))
            driver.set_page_load_timeout(45)
            driver.set_script_timeout(30)
            _USE_UC = True
            return driver
        except Exception as e:
            from config import logger

            logger.warning("undetected-chromedriver failed (%s: %s) — falling back to plain Selenium.", type(e).__name__, e)
            _USE_UC = False

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1366,2000")
    opts.add_argument(f"user-agent={UA}")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(60)  # Amazon regularly exceeds 45s from here
    driver.set_script_timeout(30)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def detect_block(driver, extra_phrases: Optional[list[str]] = None) -> bool:
    """Generic CAPTCHA/block detector. extra_phrases lets each site add its
    own known block-page wording on top of the common ones below."""
    from selenium.webdriver.common.by import By

    phrases = ["recaptcha", "please verify you are a human", "not a robot"] + (extra_phrases or [])
    try:
        url = (driver.current_url or "").lower()
        if "captcha" in url:
            return True
        body = driver.find_element(By.TAG_NAME, "body").text.lower()
        page = driver.page_source.lower()
        return any(p in body or p in page for p in phrases)
    except Exception:
        return False


def scroll_to_load_more(driver, *, rounds: int = 8, stop_when_over: int = 50, item_xpath: str = "//a[contains(@href, '/p/')]"):
    """Shared lazy-load scroller — same bounce-up-then-down pattern the old
    Flipkart code used to coax more result cards in. item_xpath is what it
    counts to decide "enough cards loaded, stop scrolling"."""
    from selenium.webdriver.common.by import By

    for _ in range(rounds):
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(random.uniform(1.0, 2.0))
            if len(driver.find_elements(By.XPATH, item_xpath)) > stop_when_over:
                break
            driver.execute_script("window.scrollBy(0, -600)")
            time.sleep(random.uniform(0.5, 1.0))
        except Exception:
            break
