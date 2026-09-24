"""
Part 4 — generic scraper.

The fallback for any domain without a dedicated scraper: brand sites,
anything Part 6's retry step (expand_search) turns up, anything not
amazon.in/flipkart.com. Unlike amazon.py/flipkart.py this takes a single
URL, not a search query — there's no general "search this site" shortcut
for an arbitrary domain, so this always operates on one candidate page at
a time (matches the original per-URL scrape(url) contract).

Uses plain requests first, not Selenium — most brand/smaller retail sites
aren't as aggressively bot-gated as Amazon/Flipkart. If a particular site
turns out to need a real browser too, that's a per-domain decision to make
once you actually see it fail, not something to build defensively now.

Always captures raw visible text regardless of whether structured parsing
succeeded — that's what Part 2's verify_or_extract leans on when this is
the only signal available (an untemplated site with no og: tags).
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from config import USER_AGENT, REQUEST_TIMEOUT_SECONDS, logger
from gem_extractor import classify_price
from scrapers.base import CandidateResult

RAW_TEXT_CHAR_CAP = 5000  # keep what's fed to the LLM later to a sane size


def _meta_content(soup: BeautifulSoup, prop: str) -> str:
    for attr in ("property", "name"):
        tag = soup.find("meta", attrs={attr: prop})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def scrape(url: str, *, session: Optional[requests.Session] = None) -> CandidateResult:
    # removeprefix, not replace: str.replace("www.", "") also strips a
    # mid-host occurrence ("awww.com" -> "a.com").
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    sess = session or requests.Session()

    try:
        resp = sess.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("generic scrape failed for %s: %s: %s", url, type(e).__name__, e)
        return CandidateResult(
            source_domain=domain,
            url=url,
            scraper_used="generic",
            structured_fields=None,
            raw_page_text=None,
            fetch_error=f"{type(e).__name__}: {e}",
        )

    soup = BeautifulSoup(resp.text, "lxml")

    title = _meta_content(soup, "og:title") or (soup.title.get_text(strip=True) if soup.title else "")
    price_text = (
        _meta_content(soup, "og:price:amount")
        or _meta_content(soup, "product:price:amount")
        or _meta_content(soup, "twitter:data1")  # some sites stash price here
    )
    price_value, _ = classify_price(price_text) if price_text else (None, "unknown")
    image_url = _meta_content(soup, "og:image") or None

    # Currency from the page's own meta — do NOT blindly claim INR. A
    # logitech.com/en-us page listing "39.99" (USD) once reached the matcher
    # tagged as INR and beat every real ₹ listing as "cheapest match".
    currency_raw = (
        _meta_content(soup, "og:price:currency")
        or _meta_content(soup, "product:price:currency")
        or _meta_content(soup, "priceCurrency")
    ).upper()
    if currency_raw in ("", "INR", "RS", "₹", "RUPEES", "INR RUPEE"):
        currency = "INR"
    elif currency_raw:
        currency = currency_raw
    else:
        currency = "INR"

    # strip script/style before dumping visible text, or the LLM gets junk
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    raw_text = soup.get_text(" ", strip=True)[:RAW_TEXT_CHAR_CAP]

    structured = None
    if title or price_value is not None:
        structured = {
            "title": title,
            "price": price_value,
            "currency": currency,
            "seller": None,
            "image_url": image_url,
            "in_stock": None,  # generic scrape has no reliable stock signal
        }

    if not structured:
        logger.info("generic scrape got no og:/meta signal from %s — relying on raw_page_text only.", url)

    return CandidateResult(
        source_domain=domain,
        url=url,
        scraper_used="generic",
        structured_fields=structured,
        raw_page_text=raw_text,
        fetch_error=None,
    )


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) != 2:
        print("Usage: python -m scrapers.generic <url>")
        sys.exit(1)

    print(json.dumps(scrape(sys.argv[1]).to_dict(), indent=2, ensure_ascii=False))
