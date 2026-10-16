"""A pretend pipeline run, for building and testing the frontend.

It emits the same events, in the same order, as the real pipeline, with
made-up products and short pauses. No network, no LLM, no browser.

    GEM_DEMO=1 python -m uvicorn api.server:app --port 8000

GEM_DEMO_SPEED=0 removes the pauses (the tests use that); 2 makes it twice
as slow, and so on.
"""
from __future__ import annotations

import os
import time

from core import events

DEMO_URL_TITLE = "Logitech M650 Wireless Mouse (Demo)"
GEM_PRICE = 1299.0


def _pause(seconds: float) -> None:
    speed = float(os.getenv("GEM_DEMO_SPEED", "1"))
    if speed > 0:
        time.sleep(seconds * speed)


def demo_runner(gem_url: str, refresh: bool, on_event) -> dict:
    with events.capture(on_event):
        e = events.emit
        e("run_started", gem_url=gem_url, refresh=refresh)

        e("stage", name="extract", label="Reading the GeM product page")
        _pause(1.5)
        e("gem_product", title=DEMO_URL_TITLE, price=GEM_PRICE, price_type="fixed", brand="Logitech",
          model="M650", category="computer-mouse", specs={"Connectivity": "Bluetooth", "Colour": "Graphite"})

        e("stage", name="normalize", label="Working out what the product really is")
        _pause(0.8)
        e("llm_call", endpoint="nvidia/nemotron-3-super-120b-a12b", provider="nvidia",
          model="nemotron-3-super-120b-a12b", purpose="normalize", seconds=2.4, ok=True)
        e("normalized", canonical_name="Logitech M650 Signature Wireless Mouse", brand="Logitech", model="M650",
          queries=["logitech m650 wireless mouse", "logitech m650 bluetooth mouse"],
          key_identifiers=["Logitech", "M650"])

        flipkart_items = [
            {"title": "Logitech M650 Wireless Mouse", "price": 1349.0, "url": "https://www.flipkart.com/demo-1", "domain": "flipkart.com"},
            {"title": "Logitech M650 L Left Wireless Mouse", "price": 1799.0, "url": "https://www.flipkart.com/demo-2", "domain": "flipkart.com"},
            {"title": "Logitech M221 Silent Wireless Mouse", "price": 699.0, "url": "https://www.flipkart.com/demo-3", "domain": "flipkart.com"},
        ]
        amazon_items = [
            {"title": "Logitech M650 Signature Wireless Mouse", "price": 1199.0, "url": "https://www.amazon.in/demo-1", "domain": "amazon.in"},
            {"title": "Logitech M650 Wireless Mouse, Graphite", "price": 1249.0, "url": "https://www.amazon.in/demo-2", "domain": "amazon.in"},
        ]
        for label, name, query, items in (
            ("flipkart", "Searching Flipkart", "logitech m650 wireless mouse", flipkart_items),
            ("amazon", "Searching Amazon", "logitech m650 wireless mouse", amazon_items),
        ):
            e("stage", name=f"search_{label}", label=name)
            e("search_started", marketplace=label, query=query)
            _pause(2.0)
            e("search_finished", marketplace=label, query=query, count=len(items), blocked=False, error=None, candidates=items)

        e("stage", name="discovery", label="Looking for other shops")
        _pause(1.0)
        e("discovery_finished", queries=["logitech m650 wireless mouse"], urls=["https://www.example-shop.in/m650"])
        e("scrape_started", url="https://www.example-shop.in/m650")
        _pause(1.0)
        other = {"title": "Logitech M650 Wireless Mouse", "price": 1450.0, "url": "https://www.example-shop.in/m650",
                 "domain": "example-shop.in", "error": None}
        e("scrape_finished", **other)

        everything = flipkart_items + amazon_items + [other]
        e("stage", name="match", label="Checking which listings are the same product")
        e("candidates_total", count=len(everything))
        verdicts = {
            "flipkart-1": (True, 0.93, "Same brand and model, price in range."),
            "flipkart-2": (False, 0.9, "Left-handed variant, not the same product."),
            "flipkart-3": (False, 0.95, "Different model (M221)."),
            "amazon-1": (True, 0.96, "Same model, Signature edition is the M650."),
            "amazon-2": (True, 0.9, "Same model, colour differs only."),
            "other-1": (True, 0.82, "Same model number on the page."),
        }
        keys = ["flipkart-1", "flipkart-2", "flipkart-3", "amazon-1", "amazon-2", "other-1"]
        matches = []
        for position, (key, item) in enumerate(zip(keys, everything), start=1):
            e("candidate_checking", url=item["url"], domain=item["domain"], title=item["title"],
              price=item["price"], index=position, total=len(everything))
            _pause(0.9)
            is_match, confidence, reason = verdicts[key]
            provider = "nvidia" if position % 3 else "backup"
            if position == 4:
                e("llm_call", endpoint="nvidia/nemotron-3-super-120b-a12b", provider="nvidia",
                  model="nemotron-3-super-120b-a12b", purpose="verify", seconds=0.4, ok=False, status=429,
                  error="RateLimitError: 429 too many requests")
                e("llm_cooldown", endpoint="nvidia/nemotron-3-super-120b-a12b", seconds=30.0, reason="429")
                provider = "backup"
            e("llm_call", endpoint=f"{provider}/demo-model", provider=provider, model="demo-model",
              purpose="verify", seconds=1.7, ok=True)
            e("candidate_verdict", url=item["url"], domain=item["domain"], is_match=is_match,
              confidence=confidence, reason=reason, price=item["price"] if is_match else None)
            if is_match:
                matches.append({"candidate_url": item["url"], "source_domain": item["domain"], "is_match": True,
                                "confidence": confidence, "reason": reason, "price_used": item["price"],
                                "extraction_source": "scraper", "delta_vs_gem": item["price"] - GEM_PRICE,
                                "cheaper_than_gem": item["price"] < GEM_PRICE})
        matches.sort(key=lambda m: m["price_used"])

        cheapest = matches[0]["price_used"]
        result = {
            "gem_product": {"url": gem_url, "title": DEMO_URL_TITLE, "gem_price": GEM_PRICE, "gem_price_type": "fixed",
                            "brand": "Logitech", "model": "M650", "category_slug": "computer-mouse", "specs": {}},
            "matches": matches,
            "not_found_on": [],
            "search_issues": [],
            "skipped": [],
            "warnings": [],
            "comparison": {"gem_price": GEM_PRICE, "gem_price_type": "fixed", "cheapest_match_price": cheapest,
                           "savings_vs_gem": GEM_PRICE - cheapest},
            "from_cache": False,
            "trace_id": "0000000000demo0",
        }
        e("run_finished", from_cache=False, matches=len(matches), cheapest=cheapest,
          savings=GEM_PRICE - cheapest, trace_id=result["trace_id"])
        return result
