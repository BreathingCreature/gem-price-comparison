"""The search half of the run: direct shop searches, search health, discovery.

Extracted from core/pipeline.py (Stage 7). Everything here is called by the
pipeline's lanes; tests call some of these helpers directly, so the names are
re-exported from core.pipeline.
"""
from __future__ import annotations

from functools import partial
from typing import Optional

import discovery
from core import events, prefilter
from core.candidates import cand_dict as _cand_dict
from core.candidates import listing_identity as _listing_key
from core.config import logger
from core.parallel import run_many, run_pair
from scrapers import generic as generic_scraper

MAX_GENERIC_CANDIDATES = 8  # cap how many brand-site/discovery URLs get scraped per run
DIRECT_SEARCH_MIN_CANDIDATES = 5


def brief(candidate) -> dict:
    """The few fields of a candidate worth showing in a progress event."""
    d = _cand_dict(candidate)
    if not isinstance(d, dict):
        return {}
    fields = d.get("structured_fields")
    if not isinstance(fields, dict):
        fields = {}
    return {
        "title": fields.get("title"),
        "price": fields.get("price"),
        "url": d.get("url"),
        "domain": d.get("source_domain"),
        "error": d.get("fetch_error"),
    }


def search_direct(
    search_fn,
    queries: list[str],
    *,
    min_candidates: int = DIRECT_SEARCH_MIN_CANDIDATES,
    label: str = "",
    normalized=None,
):
    """Run a direct marketplace search over normalized queries until one
    returns >= min_candidates RELEVANT candidates (or queries run out).

    C7: the count is of RELEVANT candidates (titles that overlap the
    product's words, core/prefilter.py) when a normalized product is given.
    Without a normalized product every candidate counts.

    Returns (results, all_candidates): one SearchScrapeResult per query
    actually run, and the cross-query URL-deduped candidate list."""
    results = []
    seen: set[str] = set()
    all_candidates = []
    product = prefilter.product_tokens(normalized) if normalized else None
    relevant = 0
    for q in queries:
        events.emit("search_started", marketplace=label, query=q)
        r = search_fn(q)
        results.append(r)
        found = getattr(r, "candidates", None) or []
        events.emit(
            "search_finished",
            marketplace=label,
            query=q,
            count=len(found),
            blocked=bool(getattr(r, "blocked", False)),
            error=getattr(r, "error", None),
            candidates=[brief(c) for c in found][:20],
        )
        for c in getattr(r, "candidates", None) or []:
            d = _cand_dict(c)
            key = _listing_key(d.get("url")) if isinstance(d, dict) else ""
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            all_candidates.append(c)
            if product is None or prefilter.relevant_candidate(d, product):
                relevant += 1
        if relevant >= min_candidates:
            break
    return results, all_candidates


def search_marketplaces(
    flipkart_search_fn,
    amazon_search_fn,
    queries: list[str],
    *,
    again: bool = False,
    normalized=None,
    skip_flipkart: bool = False,
    skip_amazon: bool = False,
    parallel: bool = True,
):
    """Search Flipkart and Amazon, at the same time when `parallel` is true.

    A skipped shop emits a `*_skipped` stage and contributes nothing.
    Returns (flipkart_results, flipkart_candidates, amazon_results, amazon_candidates)."""
    kind = "retry" if again else "search"
    suffix = " again" if again else ""
    if skip_flipkart or skip_amazon:
        lanes = (("flipkart", flipkart_search_fn, skip_flipkart), ("amazon", amazon_search_fn, skip_amazon))
        out: dict = {}
        for name, fn, skip in lanes:
            if skip:
                events.emit("stage", name=f"{kind}_{name}_skipped", label=f"{name.title()} was not retried")
                out[name] = ([], [])
            else:
                events.emit("stage", name=f"{kind}_{name}", label=f"Searching {name.title()}{suffix}")
                out[name] = search_direct(fn, queries, label=name, normalized=normalized)
        f_results, f_candidates = out["flipkart"]
        a_results, a_candidates = out["amazon"]
    elif parallel:
        events.emit("stage", name=f"{kind}_both", label=f"Searching Flipkart and Amazon at the same time{suffix}")
        (f_results, f_candidates), (a_results, a_candidates) = run_pair(
            lambda: search_direct(flipkart_search_fn, queries, label="flipkart", normalized=normalized),
            lambda: search_direct(amazon_search_fn, queries, label="amazon", normalized=normalized),
            parallel=True,
        )
    else:
        events.emit("stage", name=f"{kind}_flipkart", label=f"Searching Flipkart{suffix}")
        f_results, f_candidates = search_direct(flipkart_search_fn, queries, label="flipkart", normalized=normalized)
        events.emit("stage", name=f"{kind}_amazon", label=f"Searching Amazon{suffix}")
        a_results, a_candidates = search_direct(amazon_search_fn, queries, label="amazon", normalized=normalized)
    return f_results, f_candidates, a_results, a_candidates


def only_blocks(results) -> bool:
    """Did this shop answer with nothing but a block? Any listing means it
    answered, and no result at all is not a block either."""
    blocked_any = False
    for r in results:
        d = _cand_dict(r)
        if not isinstance(d, dict):
            continue
        if d.get("candidates"):
            return False
        if d.get("blocked"):
            blocked_any = True
    return blocked_any


def record_search_health(result, tried_domains: set, search_issues: list) -> None:
    """A blocked/errored search is a SCRAPE FAILURE, not 'product not sold
    here' - previously both looked identical in not_found_on."""
    d = _cand_dict(result)
    domain = d.get("source_domain", "?")
    if d.get("blocked"):
        search_issues.append(f"{domain}: blocked/CAPTCHA - scrape failed (not counted as 'not found')")
    elif d.get("error"):
        search_issues.append(f"{domain}: search error: {d['error']}")
    else:
        tried_domains.add(domain)


def run_discovery_and_generic_scrape(
    queries: list[str],
    allowed_domains: Optional[list[str]],
    *,
    discover_fn,
    generic_scrape_fn,
) -> tuple[list, list]:
    """Returns (discovery_results, generic_candidates)."""
    discovery_results = discover_fn(queries, allowed_domains)
    dicts = [dr.to_dict() if hasattr(dr, "to_dict") else dr for dr in discovery_results]

    # Round-robin across queries before applying the URL cap: first-come
    # order let query1's results starve query2-3 entirely out of the
    # MAX_GENERIC_CANDIDATES budget.
    lanes = [list(d.get("filtered_urls", [])) for d in dicts if isinstance(d, dict)]
    urls: list[str] = []
    while any(lanes) and len(urls) < MAX_GENERIC_CANDIDATES:
        for lane in lanes:
            if not lane or len(urls) >= MAX_GENERIC_CANDIDATES:
                continue
            u = lane.pop(0)
            if u not in urls:
                urls.append(u)

    events.emit("discovery_finished", queries=list(queries), urls=urls)

    def _scrape_one(url: str):
        events.emit("scrape_started", url=url)
        try:
            scraped = generic_scrape_fn(url)
        except Exception as e:
            logger.warning("generic scrape raised for %s: %s: %s - skipping.", url, type(e).__name__, e)
            return None
        events.emit("scrape_finished", **brief(scraped))
        return scraped

    # Plain HTTP, no browser - the (up to 8) brand-site pages are fetched at
    # the same time instead of one after another. One bad page still only
    # skips itself.
    jobs = [partial(_scrape_one, u) for u in urls]
    scraped_pages = run_many(jobs, workers=len(jobs)) if jobs else []
    generic_candidates = [page for page in scraped_pages if page is not None]

    return discovery_results, generic_candidates
