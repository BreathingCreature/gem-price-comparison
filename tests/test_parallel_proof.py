"""Proof that the three first-pass lanes and the shop pages really overlap.

Short scaled delays stand in for the slow real searches. The whole test runs
in well under three seconds and never touches the network.
"""
import threading
import time

from core import events, pipeline
from llm.match import MatchBatchResult
from scrapers._base import CandidateResult, SearchScrapeResult

NORMALIZED = {
    "canonical_name": "Demo Mouse",
    "brand": "Demo",
    "model": None,
    "key_identifiers": ["Demo"],
    "search_queries": ["demo mouse"],
    "gem_price": 1000.0,
}
PAGES = [
    "https://croma.com/demo-mouse",
    "https://cashify.in/demo-mouse",
    "https://reliancedigital.in/demo-mouse",
    "https://indiamart.com/demo-mouse",
]


def _candidate(domain, url):
    return CandidateResult(
        source_domain=domain, url=url, scraper_used="generic",
        structured_fields={"title": "Demo Mouse", "price": 900.0, "currency": "INR"},
        raw_page_text="",
    )


def test_lanes_start_together_and_each_shop_page_is_read_at_the_same_time():
    marks: dict = {}
    verify_starts: dict = {}
    page_windows: list = []
    lock = threading.Lock()
    page_barrier = threading.Barrier(len(PAGES), timeout=2)

    def mark(name):
        with lock:
            marks.setdefault(name, time.monotonic())

    def search(domain, delay):
        def fn(query):
            mark(domain + " search start")
            time.sleep(delay)
            mark(domain + " search end")
            return SearchScrapeResult(
                source_domain=domain, query=query,
                candidates=[_candidate(domain, "https://" + domain + "/demo-mouse")],
            )
        return fn

    def discover_fn(queries, allowed_domains):
        mark("other search start")
        time.sleep(0.2)
        mark("other search end")
        return [{
            "query": queries[0], "raw_urls": list(PAGES), "filtered_urls": list(PAGES),
            "blocked": False, "error": None,
        }]

    def generic_scrape_fn(url):
        # If the pages were read one after another the first one would wait
        # here until the barrier times out.
        page_barrier.wait()
        started = time.monotonic()
        mark("page start")
        time.sleep(0.1)
        mark("page end")
        with lock:
            page_windows.append((started, time.monotonic()))
        return _candidate(url.split("/")[2], url)

    def match_fn(normalized, candidates):
        domain = candidates[0].source_domain
        with lock:
            verify_starts.setdefault(domain, time.monotonic())
        decisions = [
            {"candidate_url": c.url, "source_domain": c.source_domain, "is_match": False,
             "confidence": 0.9, "outcome": "no_match", "price_used": 900.0}
            for c in candidates
        ]
        return MatchBatchResult(all_decisions=decisions)

    started = time.monotonic()
    with events.capture() as sink:
        lanes = pipeline._run_first_pass_lanes(
            search("flipkart.com", 0.3),
            search("amazon.in", 0.1),
            ["demo mouse"],
            discover_fn=discover_fn,
            generic_scrape_fn=generic_scrape_fn,
            normalized=NORMALIZED,
            match_fn=match_fn,
        )
    elapsed = time.monotonic() - started

    assert len(lanes) == 3
    assert elapsed < 3.0

    offsets = {}
    for event in sink.events:
        if event["type"] == "shop_started":
            offsets.setdefault(event["shop"], event["t"])
    starts = [offsets[name] for name in ("flipkart.com", "amazon.in", "other")]
    assert set(offsets) >= {"flipkart.com", "amazon.in", "other"}
    assert max(starts) - min(starts) <= 0.1

    assert verify_starts["amazon.in"] < marks["flipkart.com search end"]
    assert verify_starts["flipkart.com"] > marks["flipkart.com search end"]

    assert len(page_windows) == len(PAGES)
    window = max(end for _, end in page_windows) - min(start for start, _ in page_windows)
    assert window < 0.3  # four 0.1 s pages would take 0.4 s in sequence
