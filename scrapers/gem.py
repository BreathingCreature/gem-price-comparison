"""
Part 1 — GeM product extractor.

Input: a single GeM product URL
       (mkp.gem.gov.in/<category>/<product>/p-<id>-<id>-cat.html)
Output: a GemProduct — see the dataclass below.

No browser needed. GeM's product pages are server-rendered HTML; a real
(even headless, even anti-detection-masked) browser actually gets blocked
here. Plain requests + BeautifulSoup works. See the build spec doc, section
0, for how that was confirmed. This is the opposite situation from
Amazon/Flipkart in Part 4, which DO need a real browser — don't copy this
module's approach over there.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from core.config import USER_AGENT, REQUEST_TIMEOUT_SECONDS, logger

# --- data contract -----------------------------------------------------------


@dataclass
class GemProduct:
    url: str
    category_slug: str
    product_slug: str
    title: str
    brand: Optional[str]
    model: Optional[str]
    specs: dict
    gem_price: Optional[float]
    gem_price_type: str  # "fixed" | "range" | "L1_rate" | "unknown"
    currency: str = "INR"

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "category_slug": self.category_slug,
            "product_slug": self.product_slug,
            "title": self.title,
            "brand": self.brand,
            "model": self.model,
            "specs": self.specs,
            "gem_price": self.gem_price,
            "gem_price_type": self.gem_price_type,
            "currency": self.currency,
        }


# --- URL parsing (free, no network) -------------------------------------------

_URL_RE = re.compile(r"^/([^/]+)/([^/]+)/p-[\d-]+-cat\.html")


def parse_gem_url(url: str) -> tuple[str, str]:
    """Pull (category_slug, product_slug) straight out of the URL path.
    Returns ("", "") if the URL doesn't match GeM's known product-page
    pattern — caller decides whether that's fatal."""
    path = urlparse(url).path
    m = _URL_RE.match(path)
    if not m:
        return "", ""
    return m.group(1), m.group(2)


# --- price classification -----------------------------------------------------
# Ported from the old repo's classify_price() — same logic, same price types,
# because GeM pricing isn't always a single clean number (reverse-auction
# pricing means "range" and "L1_rate" show up regularly).

_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")
# A real range needs an actual range shape: two amounts joined by a dash
# (hyphen, en dash, em dash) or the word "to". Text like
# "Rs.2,725.00 17% You Save: ₹570.00" has several numbers but no range
# shape, and must stay a fixed price.
_RANGE_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(?:[-\u2013\u2014]|\bto\b)\s*\u20b9?\s*(\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)


def _match_to_float(match: Optional[re.Match]) -> Optional[float]:
    """float() a number match, stripping a trailing comma from it first.
    Extracted here so a malformed match can never crash the run — an
    unusable number is honestly None, not an exception."""
    if not match:
        return None
    try:
        return float(match.group(1).rstrip(",").replace(",", ""))
    except ValueError:
        return None


def classify_price(text: str) -> tuple[Optional[float], str]:
    """Return (numeric_value_or_None, price_type).
    price_type is one of: fixed | range | L1_rate | unknown."""
    t = (text or "").strip()
    low = t.lower()
    if not t:
        return None, "unknown"
    if "l1" in low or "lowest quoted" in low:
        # Strip the "L1" token itself first — otherwise the '1' in 'L1' gets
        # picked up as the price by the regex below instead of the real value.
        stripped = re.sub(r"\bl-?1\b", "", t, flags=re.IGNORECASE)
        return _match_to_float(_NUM_RE.search(stripped)), "L1_rate"
    # "Our Price: ₹2,725.00 17% You Save: ₹570.00" contains several numbers
    # but is a single fixed price — keyword forms must not be read as a range.
    if any(k in low for k in ("our price", "final price", "offer price", "our_price")):
        value = _match_to_float(_NUM_RE.search(t))
        if value is not None:
            return value, "fixed"
    m = _RANGE_RE.search(t)
    if m:
        value = _match_to_float(m)
        if value is not None:
            return value, "range"
    m = _NUM_RE.search(t)
    if m:
        value = _match_to_float(m)
        if value is not None:
            return value, "fixed"
    return None, "unknown"


# --- HTML extraction -----------------------------------------------------------
# Price selectors in priority order, proven against the live detail template
# (2026-09): the page exposes .final-price / .our_price / .offer_price /
# .add-to-cart-price — the OLD selectors below (.variant-final-price, bare
# .price) match nothing on detail pages, which is why gem_price used to come
# back null. Legacy selectors kept last for listing-style pages/tests.
# Never prefer .list_price / .strike — that's MRP, not the selling price.

_PRICE_SELECTORS_PRIORITY = (
    ".final-price",
    ".our_price",
    ".offer_price",
    ".add-to-cart-price",
    ".variant-final-price",
    ".product-price",
    ".price",
)
_NAME_SELECTORS = ".variant-desc .variant-title, .variant-title, .product-title, h1"
_SELLER_SELECTORS = ".sold_as.oem, .sold_as_summary, .seller, .brand"


def _first_text(soup: BeautifulSoup, selector: str) -> str:
    el = soup.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def _extract_price_text(soup: BeautifulSoup) -> str:
    """First priority selector whose text contains a digit. Digit check
    matters: some price elements exist as empty placeholders pre-render."""
    for selector in _PRICE_SELECTORS_PRIORITY:
        text = _first_text(soup, selector)
        if text and re.search(r"\d", text):
            return text
    return ""


def _extract_specs(soup: BeautifulSoup) -> dict:
    """Generic key:value spec-table scraper. GeM's spec block structure
    varies by category, so this looks for common table/dl row patterns
    instead of one hardcoded selector. This is a best-effort starting
    point — tighten it once you've seen a real page's actual markup."""
    specs: dict[str, str] = {}

    for row in soup.select("table tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) == 2:
            key = cells[0].get_text(" ", strip=True)
            val = cells[1].get_text(" ", strip=True)
            if key and val:
                specs[key] = val

    for dl in soup.select("dl"):
        keys = dl.find_all("dt")
        vals = dl.find_all("dd")
        for k, v in zip(keys, vals):
            kt, vt = k.get_text(" ", strip=True), v.get_text(" ", strip=True)
            if kt and vt:
                specs[kt] = vt

    return specs


def _guess_brand_model(title: str) -> tuple[Optional[str], Optional[str]]:
    """Very light heuristic: first word of the title as brand (case-
    insensitive — real GeM titles often start lowercase, e.g. "logitech
    Wireless Computer Mouse"). Intentionally weak — Part 2's LLM normalizer
    does the real identity extraction. This just gives it a head start, and
    is a fallback if that LLM call fails."""
    if not title:
        return None, None
    m = re.match(r"([A-Za-z0-9]+)", title.strip())
    if not m:
        return None, None
    word = m.group(1)
    brand = word[0].upper() + word[1:]  # "logitech" -> "Logitech", keeps "iPhone"-style casing
    return brand, None


# --- fetch with one small retry (D4) -------------------------------------------
# One timeout, reset connection or 5xx during a burst shouldn't kill a whole
# run over a failure the next attempt would have survived.

RETRY_SLEEP_SECONDS = 2.0  # between attempts; patched to 0 in tests
_MAX_FETCH_ATTEMPTS = 2
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})


def _is_retryable_fetch_error(exc: BaseException) -> bool:
    """Transient-only retry policy: ConnectionError, Timeout and HTTP
    500/502/503/504. A 404 or 403 means the page itself said no — retrying
    those just burns seconds."""
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code in _RETRYABLE_STATUS
    return False


def _fetch_gem_page(url: str, sess, headers: dict):
    """sess.get + raise_for_status with a single retry for transient
    failures. The sleep constant is module-level and the time module is
    imported here, so tests can zero the wait by patching either."""
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = sess.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            if attempt >= _MAX_FETCH_ATTEMPTS or not _is_retryable_fetch_error(exc):
                raise
            logger.warning(
                "GeM fetch failed (%s: %s), attempt %d/%d for %s — retrying in %.0fs.",
                type(exc).__name__,
                str(exc)[:120],
                attempt,
                _MAX_FETCH_ATTEMPTS,
                url,
                RETRY_SLEEP_SECONDS,
            )
            time.sleep(RETRY_SLEEP_SECONDS)


def extract_gem_product(url: str, *, session: Optional[requests.Session] = None) -> GemProduct:
    category_slug, product_slug = parse_gem_url(url)

    sess = session or requests.Session()
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"}
    resp = _fetch_gem_page(url, sess, headers)

    soup = BeautifulSoup(resp.text, "lxml")

    title = _first_text(soup, _NAME_SELECTORS)
    price_text = _extract_price_text(soup)
    seller = _first_text(soup, _SELLER_SELECTORS) or None
    specs = _extract_specs(soup)
    if seller:
        specs.setdefault("seller", seller)

    price_value, price_type = classify_price(price_text)
    brand, model = _guess_brand_model(title)

    # A page with neither a title nor a price is a block page, a captcha or
    # a changed layout — an empty product here would just burn LLM calls on
    # nothing downstream. Say so loudly instead of playing along.
    if not title and price_value is None:
        raise RuntimeError(
            "The GeM page gave no title and no price (the page may be blocked "
            "or its layout changed): " + url
        )

    if not title:
        logger.warning(
            "No title extracted from %s — selectors may not match this page's template.",
            url,
        )

    return GemProduct(
        url=url,
        category_slug=category_slug,
        product_slug=product_slug,
        title=title,
        brand=brand,
        model=model,
        specs=specs,
        gem_price=price_value,
        gem_price_type=price_type,
        currency="INR",
    )


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) != 2:
        print("Usage: python gem_extractor.py <gem_product_url>")
        sys.exit(1)

    product = extract_gem_product(sys.argv[1])
    print(json.dumps(product.to_dict(), indent=2, ensure_ascii=False))
