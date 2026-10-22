"""
Generic scraper for any shop URL.

The fallback for any domain without a dedicated scraper: brand sites,
anything the retry step (expand_search) turns up, anything not
amazon.in/flipkart.com. Unlike amazon.py/flipkart.py this takes a single
URL, not a search query.

Structured data is read in the order pages actually use it:
  1. JSON-LD (schema.org/Product, offers, @graph and lists) — the most
     reliable, because shops publish it for Google.
  2. meta tags (og:*, product:price:*) — the older fallback.
  3. h1 / <title> — title only; a price still needs one of the above.
A listing without a price is still returned (price=None, price_unknown)
instead of being dropped: the matcher can decide, and a missing price is
honest information.

Plain requests, no browser. Before and after redirects the final address
must be a public http(s) one — a link from a search engine must never be
able to make the server fetch 127.0.0.1 or the LAN.
"""
from __future__ import annotations

import ipaddress
import json
import re
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from core.config import USER_AGENT, REQUEST_TIMEOUT_SECONDS, logger
from scrapers.gem import classify_price
from scrapers._base import CandidateResult

RAW_TEXT_CHAR_CAP = 2_000_000  # limit raw text fed into LLM later

_SOFT_404_PHRASES = ("page not found", "product not found", "no longer available", "item not found")
_SOFT_404_404 = re.compile(r"\b404\b")


def _meta_content(soup: BeautifulSoup, prop: str) -> str:
    for attr in ("property", "name"):
        tag = soup.find("meta", attrs={attr: prop})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def _is_public_http_url(url: str) -> bool:
    """False for anything that is not a public http(s) address."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host or host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # a name; requests resolves it, this guard cannot
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


# --- JSON-LD ------------------------------------------------------------------


def _walk_jsonld(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)
    elif isinstance(node, dict):
        yield node
        if "@graph" in node:
            yield from _walk_jsonld(node["@graph"])


def _jsonld_nodes(soup: BeautifulSoup):
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        yield from _walk_jsonld(data)


def _find_product_node(soup: BeautifulSoup) -> Optional[dict]:
    for node in _jsonld_nodes(soup):
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(isinstance(item, str) and item.lower() == "product" for item in kinds):
            return node
    return None


def _first_text_value(value) -> Optional[str]:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _first_text_value(value.get("name") or value.get("url"))
    if isinstance(value, list):
        for item in value:
            found = _first_text_value(item)
            if found:
                return found
    return None


def _jsonld_fields(soup: BeautifulSoup) -> dict:
    node = _find_product_node(soup)
    if not node:
        return {}

    fields: dict = {}
    title = _first_text_value(node.get("name"))
    if title:
        fields["title"] = title
    image = _first_text_value(node.get("image"))
    if image:
        fields["image_url"] = image
    brand = _first_text_value(node.get("brand"))
    if brand:
        fields["brand"] = brand

    offers = node.get("offers")
    offers = offers if isinstance(offers, list) else [offers]
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        raw_price = offer.get("price")
        if raw_price is None:
            raw_price = offer.get("lowPrice")
        if "price" not in fields and raw_price is not None:
            value, _kind = classify_price(str(raw_price))
            if value is not None:
                fields["price"] = value
        currency = offer.get("priceCurrency")
        if currency and "currency" not in fields:
            fields["currency"] = str(currency).strip().upper()
        availability = str(offer.get("availability") or "").lower()
        if availability:
            fields["in_stock"] = "outofstock" not in availability and "soldout" not in availability
        if "price" in fields:
            break
    return fields


# --- soft 404 -----------------------------------------------------------------


def _is_soft_404(title: str, h1: str) -> bool:
    text = f"{title} {h1}".lower()
    if any(phrase in text for phrase in _SOFT_404_PHRASES):
        return True
    return bool(_SOFT_404_404.search(text))


def scrape(url: str, *, session: Optional[requests.Session] = None) -> CandidateResult:
    # removeprefix, not replace: str.replace("www.", "") also strips a
    # mid-host occurrence ("awww.com" -> "a.com").
    domain = urlparse(url).netloc.lower().removeprefix("www.")

    def failed(message: str) -> CandidateResult:
        return CandidateResult(
            source_domain=domain,
            url=url,
            scraper_used="generic",
            structured_fields=None,
            raw_page_text=None,
            fetch_error=message,
        )

    if not _is_public_http_url(url):
        logger.warning("generic scrape refused non-public URL %s", url)
        return failed("refused: not a public http(s) URL")

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
        return failed(f"{type(e).__name__}: {e}")

    final_url = getattr(resp, "url", url) or url
    if not _is_public_http_url(final_url):
        logger.warning("generic scrape refused a redirect to %s", final_url)
        return failed("refused: redirect to a private or non-http address")

    # Limit raw text before parsing to avoid blowing memory on huge pages.
    # resp.text is read-only on a real requests.Response, so the shortened
    # copy lives in our own variable. Assigning to resp.text raised
    # AttributeError on every live page (SCRP-01).
    html = resp.text[:RAW_TEXT_CHAR_CAP]
    soup = BeautifulSoup(html, "lxml")

    linked = _jsonld_fields(soup)

    h1_tag = soup.find("h1")
    h1_text = h1_tag.get_text(" ", strip=True) if h1_tag else ""
    meta_title = _meta_content(soup, "og:title") or (soup.title.get_text(strip=True) if soup.title else "")
    title = linked.get("title") or meta_title or h1_text

    price_text = (
        _meta_content(soup, "og:price:amount")
        or _meta_content(soup, "product:price:amount")
        or _meta_content(soup, "twitter:data1")  # some sites stash price here
    )
    price_value, _ = classify_price(price_text) if price_text else (None, "unknown")
    price = linked.get("price")
    if price is None:
        price = price_value
    image_url = linked.get("image_url") or _meta_content(soup, "og:image") or None

    # Currency from the page's own data — do NOT blindly claim INR. A
    # logitech.com/en-us page listing "39.99" (USD) once reached the matcher
    # tagged as INR and beat every real ₹ listing as "cheapest match".
    currency_raw = (
        linked.get("currency")
        or _meta_content(soup, "og:price:currency")
        or _meta_content(soup, "product:price:currency")
        or _meta_content(soup, "priceCurrency")
        or ""
    ).upper().strip()
    if currency_raw in ("", "INR", "RS", "₹", "RUPEES", "INR RUPEE"):
        currency = "INR"
        currency_assumed = not bool(currency_raw)
    else:
        currency = currency_raw
        currency_assumed = False

    page_not_found = _is_soft_404(title, h1_text)
    if page_not_found:
        logger.info("generic scrape found a soft-404 page at %s", url)

    # strip script/style before dumping visible text, or the LLM gets junk
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    raw_text = soup.get_text(" ", strip=True)[:RAW_TEXT_CHAR_CAP]

    structured = None
    if title or price is not None:
        structured = {
            "title": title,
            "price": price,
            "currency": currency,
            "seller": None,
            "image_url": image_url,
            "in_stock": linked.get("in_stock"),
        }
        if linked.get("brand"):
            structured["brand"] = linked["brand"]
        if currency_assumed:
            structured["currency_assumed"] = True
        if price is None:
            structured["price_unknown"] = True
        if page_not_found:
            structured["page_not_found"] = True

    if not structured:
        logger.info("generic scrape got no structured signal from %s — relying on raw_page_text only.", url)

    return CandidateResult(
        source_domain=domain,
        url=url,
        scraper_used="generic",
        structured_fields=structured,
        raw_page_text=raw_text,
        fetch_error=None,
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python -m scrapers.generic <url>")
        sys.exit(1)

    print(json.dumps(scrape(sys.argv[1]).to_dict(), indent=2, ensure_ascii=False))
