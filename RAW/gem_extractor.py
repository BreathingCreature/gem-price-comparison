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
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from config import USER_AGENT, REQUEST_TIMEOUT_SECONDS, logger

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

_NUM_RE = re.compile(r"([\d,]+(?:\.\d+)?)")


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
        m = _NUM_RE.search(stripped)
        return (float(m.group(1).replace(",", "")) if m else None), "L1_rate"
    nums = _NUM_RE.findall(t)
    if len(nums) >= 2:
        return float(nums[0].replace(",", "")), "range"
    if len(nums) == 1:
        return float(nums[0].replace(",", "")), "fixed"
    return None, "unknown"


# --- HTML extraction -----------------------------------------------------------
# Selectors proven on GeM's category/listing pages by prior tested code.
# Verify these against the single-product-detail template too — listing
# cards and detail pages sometimes diverge on the same site.

_NAME_SELECTORS = ".variant-desc .variant-title, .variant-title, .product-title, h1"
_PRICE_SELECTORS = ".variant-final-price, .price, .product-price"
_SELLER_SELECTORS = ".sold_as.oem, .sold_as_summary, .seller, .brand"


def _first_text(soup: BeautifulSoup, selector: str) -> str:
    el = soup.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


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
    """Very light heuristic: first capitalized word as brand. Intentionally
    weak — Part 2's LLM normalizer does the real identity extraction. This
    just gives it a head start, and is a fallback if that LLM call fails."""
    if not title:
        return None, None
    m = re.match(r"^([A-Z][a-zA-Z0-9]+)", title.strip())
    brand = m.group(1) if m else None
    return brand, None


def extract_gem_product(url: str, *, session: Optional[requests.Session] = None) -> GemProduct:
    category_slug, product_slug = parse_gem_url(url)

    sess = session or requests.Session()
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"}
    resp = sess.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")

    title = _first_text(soup, _NAME_SELECTORS)
    price_text = _first_text(soup, _PRICE_SELECTORS)
    seller = _first_text(soup, _SELLER_SELECTORS) or None
    specs = _extract_specs(soup)
    if seller:
        specs.setdefault("seller", seller)

    price_value, price_type = classify_price(price_text)
    brand, model = _guess_brand_model(title)

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
