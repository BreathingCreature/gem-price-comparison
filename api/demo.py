"""Demo mode: the REAL pipeline with fake shops, a fake LLM and a fake GeM page.

    GEM_DEMO=1 python -m uvicorn api.server:app --host 127.0.0.1 --port 8000

Every event comes from core/pipeline.py, so the page is exercised exactly as
it is in a live run: a shop cards fills in when its search ends, one shop is
blocked and then found on the retry, one shop is honestly "not found". The
demo also has one possible match (a different pack) and one B2B row so those
states can be reviewed. Short pauses make the cards fill one by one.

No network, no LLM, no browser. The demo never reads or writes the result
cache (a fake answer must not be served to a real run later).
"""
from __future__ import annotations

import time

from core import events, pipeline
from llm import match as matcher
from scrapers._base import CandidateResult, SearchScrapeResult

DEMO_URL_TITLE = "Logitech M650 Signature Wireless Mouse (Demo)"
GEM_PRICE = 1299.0
DEMO_DELAY = 0.35  # seconds per fake step; tests set this to 0

FIRST_QUERY = "logitech m650 wireless mouse"
RETRY_QUERY = "logitech m650 signature wireless mouse"
DEMO_FLIPKART_PRICE = 1149.0
DEMO_AMAZON_PRICE = 1199.0
DEMO_PACK_PRICE = 2199.0
DEMO_OTHER_PRICE = 699.0
DEMO_B2B_PRICE = 1249.0
DEMO_DEARER_PRICE = 1499.0

# A local drawing keeps the demo offline while still exercising the image
# branch in the GeM card. The page accepts data: images as well as GeM hosts.
# It is percent-encoded, not base64: the key-shaped-text redactor treats a long
# base64 run as a secret and would replace the image source with [hidden].
DEMO_IMAGE = (
    "data:image/svg+xml,"
    "%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20width%3D%22160%22%20height"
    "%3D%22160%22%20viewBox%3D%220%200%20160%20160%22%3E%3Crect%20width%3D%22160%22%20height%"
    "3D%22160%22%20fill%3D%22%23eef1f5%22%2F%3E%3Crect%20x%3D%2252%22%20y%3D%2236%22%20width%"
    "3D%2256%22%20height%3D%2288%22%20rx%3D%2228%22%20fill%3D%22%23d9dce1%22%20stroke%3D%22%2"
    "35a6270%22%20stroke-width%3D%222%22%2F%3E%3Cpath%20d%3D%22M80%2040v34%22%20stroke%3D%22%"
    "235a6270%22%20stroke-width%3D%222%22%2F%3E%3Cpath%20d%3D%22M72%2080h16%22%20stroke%3D%22"
    "%235a6270%22%20stroke-width%3D%222%22%2F%3E%3Ctext%20x%3D%2280%22%20y%3D%22140%22%20text"
    "-anchor%3D%22middle%22%20font-family%3D%22Segoe%20UI%2C%20sans-serif%22%20font-size%3D%2"
    "212%22%20fill%3D%22%235a6270%22%3EGeM%20demo%3C%2Ftext%3E%3C%2Fsvg%3E"
)
DEMO_SPECS = {
    "Connectivity": "Bluetooth 5.1 and Logi Bolt USB receiver",
    "Colour": "Graphite",
    "Sensor": "Darkfield high precision",
    "Buttons": "5 programmable buttons",
    "Battery": "1 AA battery, up to 24 months",
    "Compatible with": "Windows 10 or later, macOS 10.14 or later, Linux, iPadOS 14 or later, Android 8 or later",
    "Dimensions": "105.4 mm x 65.8 mm x 38.2 mm",
    "Weight": "101 g including battery",
    "Scroll wheel": "SmartWheel with horizontal scrolling",
    "Silent clicks": "Yes, quiet click switches",
    "Wireless range": "10 m",
    "Package contents": "Mouse, USB receiver, AA battery, user documentation",
    "Warranty": "1 year limited hardware warranty",
    "Country of origin": "China",
    "Model number": "910-006259",
    "Part number": "M650-GRAPHITE-DEMO",
}
DEMO_OTHER_SHOPS = (
    ("example-shop.in", "https://www.example-shop.in/m221", "Logitech M221 Silent Wireless Mouse", DEMO_OTHER_PRICE),
    ("indiamart.com", "https://www.indiamart.com/demo-m650", "Logitech M650 Signature Wireless Mouse", DEMO_B2B_PRICE),
    ("reliancedigital.in", "https://www.reliancedigital.in/demo-m650", "Logitech M650 Signature Wireless Mouse", DEMO_DEARER_PRICE),
    ("croma.com", "https://www.croma.com/demo-m650", None, None),
)


def _pause(seconds: float = DEMO_DELAY) -> None:
    if DEMO_DELAY > 0:
        time.sleep(min(seconds, DEMO_DELAY))


def _gem_product(gem_url: str) -> dict:
    return {
        "url": gem_url,
        "title": DEMO_URL_TITLE,
        "gem_price": GEM_PRICE,
        "gem_price_type": "fixed",
        "brand": "Logitech",
        "model": "M650",
        "category_slug": "computer-mouse",
        "image": DEMO_IMAGE,
        "specs": dict(DEMO_SPECS),
    }


def _normalize(product: dict) -> dict:
    _pause()
    events.emit(
        "llm_call",
        endpoint="demo/normalize-model",
        provider="demo",
        model="demo-model",
        purpose="normalize",
        seconds=round(DEMO_DELAY, 2),
        ok=True,
    )
    _pause()
    return {
        "canonical_name": "Logitech M650 Signature Wireless Mouse",
        "brand": "Logitech",
        "model": "M650",
        "search_queries": [FIRST_QUERY, "logitech m650 bluetooth mouse"],
        "key_identifiers": ["Logitech", "M650"],
        "gem_price": GEM_PRICE,
        "described_by": "llm",
    }


def _candidate(domain: str, url: str, title: str, price: float, *, error: str = "") -> CandidateResult:
    return CandidateResult(
        source_domain=domain,
        url=url,
        scraper_used=domain.split(".")[0],
        structured_fields=None if error else {"title": title, "price": price, "currency": "INR", "in_stock": True},
        raw_page_text=None,
        fetch_error=error or None,
    )


def _blocked(domain: str, query: str) -> SearchScrapeResult:
    _pause()
    return SearchScrapeResult(
        source_domain=domain,
        query=query,
        candidates=[],
        blocked=True,
        status="blocked",
        reason="demo: the site showed a CAPTCHA",
    )


def _flipkart_search(query: str) -> SearchScrapeResult:
    if query != RETRY_QUERY:
        return _blocked("flipkart.com", query)
    _pause()
    return SearchScrapeResult(
        source_domain="flipkart.com",
        query=query,
        candidates=[
            _candidate("flipkart.com", "https://www.flipkart.com/demo-m650", "Logitech M650 Wireless Mouse", DEMO_FLIPKART_PRICE)
        ],
        pages_scraped=1,
        status="ok",
    )


def _amazon_search(query: str) -> SearchScrapeResult:
    _pause()
    return SearchScrapeResult(
        source_domain="amazon.in",
        query=query,
        candidates=[
            _candidate(
                "amazon.in",
                "https://www.amazon.in/demo-m650",
                "Logitech M650 Signature Wireless Mouse",
                DEMO_AMAZON_PRICE,
            ),
            _candidate(
                "amazon.in",
                "https://www.amazon.in/demo-m650-pack2",
                "Logitech M650 Signature Wireless Mouse (Pack of 2)",
                DEMO_PACK_PRICE,
            ),
            _candidate("amazon.in", "https://www.amazon.in/demo-m221", "Logitech M221 Silent Wireless Mouse", 799.0),
        ],
        pages_scraped=1,
        status="ok",
    )


def _discover(queries, allowed_domains):
    _pause()
    from discovery.discover import DiscoveryResult

    urls = [entry[1] for entry in DEMO_OTHER_SHOPS]
    return [
        DiscoveryResult(
            query=queries[0] if queries else FIRST_QUERY,
            raw_urls=urls,
            filtered_urls=urls,
        )
    ]


def _generic_scrape(url: str) -> CandidateResult:
    _pause()
    for domain, page_url, title, price in DEMO_OTHER_SHOPS:
        if page_url == url:
            if title is None:
                return _candidate(domain, url, "", 0.0, error="demo: the shop blocked the page")
            return _candidate(domain, url, title, price)
    return _candidate("example-shop.in", url, "Logitech M221 Silent Mouse", DEMO_OTHER_PRICE)


def _expand_search(normalized, queries, domains):
    _pause()
    events.emit(
        "llm_call",
        endpoint="demo/expand-model",
        provider="demo",
        model="demo-model",
        purpose="expand",
        seconds=round(DEMO_DELAY, 2),
        ok=True,
    )
    return {"new_queries": [RETRY_QUERY], "suggested_domains": [], "notes": "demo: one more try"}


def _demo_match(normalized, candidates, **_kwargs) -> matcher.MatchBatchResult:
    """A fake verifier that follows the prompt rules: same model number and a
    sensible price means "match", a different model means "no match", and a
    different pack of the same model is a "possible" match. It emits the same
    checking/verdict events the real matcher emits."""
    decisions = []
    confirmed = []
    possible = []
    total = len(candidates)
    for position, candidate in enumerate(candidates, start=1):
        data = candidate.to_dict() if hasattr(candidate, "to_dict") else candidate
        fields = data.get("structured_fields") if isinstance(data.get("structured_fields"), dict) else {}
        title = fields.get("title") or ""
        price = fields.get("price")
        is_pack = "pack of 2" in title.lower()
        is_match = "M650" in title and "M221" not in title and not is_pack
        if is_pack:
            outcome = "possible"
            confidence = 0.62
            reason = "Different pack: this price is for two mice."
            why = "different pack"
        elif is_match:
            outcome = "match"
            confidence = 0.94
            reason = "Same model number and a plausible price."
            why = ""
        else:
            outcome = "no_match"
            confidence = 0.9
            reason = "Different model (M221)."
            why = ""
        events.emit(
            "candidate_checking",
            url=data.get("url"),
            domain=data.get("source_domain"),
            title=title,
            price=price,
            index=position,
            total=total,
        )
        _pause()
        events.emit(
            "llm_call",
            endpoint="demo/verify-model",
            provider="demo",
            model="demo-model",
            purpose="verify",
            seconds=round(DEMO_DELAY, 2),
            ok=True,
        )
        decision = {
            "candidate_url": data.get("url"),
            "source_domain": data.get("source_domain"),
            "is_match": is_match,
            "confidence": confidence,
            "reason": reason,
            "price_used": price,
            "extraction_source": "scraper",
            "structured_fields": fields,
            "outcome": outcome,
        }
        if why:
            decision["why"] = why
        decisions.append(decision)
        if outcome == "match":
            confirmed.append(decision)
        elif outcome == "possible":
            possible.append(decision)
        events.emit(
            "candidate_verdict",
            url=data.get("url"),
            domain=data.get("source_domain"),
            is_match=is_match,
            confidence=confidence,
            reason=reason,
            price=price,
            outcome=outcome,
        )
    confirmed.sort(key=lambda d: d["price_used"])
    possible.sort(key=lambda d: (d.get("price_used") is None, d.get("price_used") or 0.0))
    return matcher.MatchBatchResult(
        all_decisions=decisions,
        confirmed_matches=confirmed,
        skipped=[],
        possible_matches=possible,
        not_checked=[],
    )


def demo_runner(gem_url: str, refresh: bool, on_event) -> dict:
    """One fake comparison through the real pipeline (same events, same
    result shape, same retry rules). The demo never caches."""
    return pipeline.run_pipeline(
        gem_url,
        refresh=refresh,
        on_event=on_event,
        extract_fn=_gem_product,
        normalize_fn=_normalize,
        flipkart_search_fn=_flipkart_search,
        amazon_search_fn=_amazon_search,
        discover_fn=_discover,
        generic_scrape_fn=_generic_scrape,
        match_fn=_demo_match,
        expand_search_fn=_expand_search,
        use_cache=False,
    )
