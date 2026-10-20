"""Demo mode: the REAL pipeline with fake shops, a fake LLM and a fake GeM page.

    GEM_DEMO=1 python -m uvicorn api.server:app --host 127.0.0.1 --port 8000

Every event comes from core/pipeline.py, so the page is exercised exactly as
it is in a live run: a shop cards fills in when its search ends, one shop is
blocked and then found on the retry, one shop is honestly "not found". Short
pauses make the cards fill one by one instead of all at once.

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
DEMO_OTHER_PRICE = 699.0


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
        "specs": {"Connectivity": "Bluetooth", "Colour": "Graphite"},
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


def _candidate(domain: str, url: str, title: str, price: float) -> CandidateResult:
    return CandidateResult(
        source_domain=domain,
        url=url,
        scraper_used=domain.split(".")[0],
        structured_fields={"title": title, "price": price, "currency": "INR", "in_stock": True},
        raw_page_text=None,
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
            _candidate("amazon.in", "https://www.amazon.in/demo-m221", "Logitech M221 Silent Wireless Mouse", 799.0),
        ],
        pages_scraped=1,
        status="ok",
    )


def _discover(queries, allowed_domains):
    _pause()
    from discovery.discover import DiscoveryResult

    return [
        DiscoveryResult(
            query=queries[0] if queries else FIRST_QUERY,
            raw_urls=["https://www.example-shop.in/m221"],
            filtered_urls=["https://www.example-shop.in/m221"],
        )
    ]


def _generic_scrape(url: str) -> CandidateResult:
    _pause()
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
    sensible price means "match", a different model means "no match". It emits
    the same checking/verdict events the real matcher emits."""
    decisions = []
    confirmed = []
    total = len(candidates)
    for position, candidate in enumerate(candidates, start=1):
        data = candidate.to_dict() if hasattr(candidate, "to_dict") else candidate
        fields = data.get("structured_fields") if isinstance(data.get("structured_fields"), dict) else {}
        title = fields.get("title") or ""
        price = fields.get("price")
        is_match = "M650" in title and "M221" not in title
        confidence = 0.94 if is_match else 0.9
        reason = "Same model number and a plausible price." if is_match else "Different model (M221)."
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
            "price_used": price if is_match else None,
            "extraction_source": "scraper",
            "structured_fields": fields,
            "outcome": "match" if is_match else "no_match",
        }
        decisions.append(decision)
        if is_match:
            confirmed.append(decision)
        events.emit(
            "candidate_verdict",
            url=data.get("url"),
            domain=data.get("source_domain"),
            is_match=is_match,
            confidence=confidence,
            reason=reason,
            price=price if is_match else None,
        )
    confirmed.sort(key=lambda d: d["price_used"])
    return matcher.MatchBatchResult(
        all_decisions=decisions,
        confirmed_matches=confirmed,
        skipped=[],
        possible_matches=[],
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
