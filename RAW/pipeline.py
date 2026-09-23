"""
Part 6 — orchestrator.

run_pipeline(gem_url) is the one function everything else in this project
exists to support: GeM URL in, price comparison out. Sequence:

  extract (1) -> normalize (2) -> [flipkart.search + amazon.search (4)] and
  [discover (3) -> generic.scrape (4)] in parallel-ish -> match (5) ->
  if zero confirmed matches: expand_search (2) -> retry discovery+generic
  once, re-match -> cache + trace -> return.

Every external collaborator (extract_fn, normalize_fn, the two search
functions, discover_fn, generic_scrape_fn, match_fn, expand_search_fn) is
an overridable keyword argument defaulting to the real implementation.
That's not incidental — it's what makes this module's own orchestration
logic (sequencing, the retry trigger, cache, trace shape, not_found_on
computation) testable in isolation from whether the real scrapers/LLM
calls work, the same way the previous parts were tested independently.

Scope note on the retry step (your point 3, from early on): it only
re-runs discovery + generic scraping with the LLM's suggested queries/
domains, NOT a second Flipkart/Amazon search. That matches what you
actually asked for — surfacing sites the dedicated scrapers don't cover —
rather than just re-asking the same two marketplaces differently.

Scope note on the trace: it captures structured input/output at each
stage (queries used, candidates found, match decisions with their
reasons) via the to_dict() every Part 1-5 object already has. It does NOT
capture the raw LLM prompt/response text — doing that would mean
reworking Part 2's already-tested functions to expose it, which felt like
scope creep on an already-built, working part. The "reason" field on each
match decision is the closest thing to "why" without that deeper
plumbing. Worth adding later if the trace viewer turns out to need it.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import CACHE_DIR, CACHE_TTL_SECONDS, TRACES_DIR, KNOWN_MARKETPLACE_DOMAINS, logger
import gem_extractor
import llm_client
import discovery
import matcher
from scrapers import flipkart as flipkart_scraper
from scrapers import amazon as amazon_scraper
from scrapers import generic as generic_scraper

MAX_GENERIC_CANDIDATES = 8  # cap how many brand-site/discovery URLs get scraped per run


# --- cache ------------------------------------------------------------------------

def _cache_path(gem_url: str) -> Path:
    key = hashlib.sha256(gem_url.strip().encode("utf-8")).hexdigest()[:24]
    return CACHE_DIR / f"{key}.json"


def _cache_get(gem_url: str) -> Optional[dict]:
    path = _cache_path(gem_url)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Cache read failed for %s: %s — treating as a miss.", gem_url, e)
        return None
    if time.time() - payload.get("cached_at", 0) > CACHE_TTL_SECONDS:
        return None
    return payload.get("result")


def _cache_set(gem_url: str, result: dict) -> None:
    path = _cache_path(gem_url)
    try:
        path.write_text(
            json.dumps({"cached_at": time.time(), "result": result}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("Cache write failed for %s: %s — continuing without caching this result.", gem_url, e)


# --- trace ------------------------------------------------------------------------

def _write_trace(trace: dict) -> str:
    trace_id = trace["trace_id"]
    path = TRACES_DIR / f"{trace_id}.json"
    try:
        path.write_text(json.dumps(trace, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    except OSError as e:
        logger.warning("Trace write failed for %s: %s — pipeline result is still valid, just unlogged.", trace_id, e)
    return trace_id


# --- helpers ------------------------------------------------------------------------

def _domain_of_candidate(candidate) -> str:
    d = candidate.to_dict() if hasattr(candidate, "to_dict") else candidate
    return d.get("source_domain", "")


def _run_discovery_and_generic_scrape(
    queries: list[str],
    allowed_domains: Optional[list[str]],
    *,
    discover_fn,
    generic_scrape_fn,
) -> tuple[list, list]:
    """Returns (discovery_results, generic_candidates)."""
    discovery_results = discover_fn(queries, allowed_domains)
    urls: list[str] = []
    for dr in discovery_results:
        d = dr.to_dict() if hasattr(dr, "to_dict") else dr
        for u in d.get("filtered_urls", []):
            if u not in urls:
                urls.append(u)
    urls = urls[:MAX_GENERIC_CANDIDATES]

    generic_candidates = []
    for url in urls:
        try:
            generic_candidates.append(generic_scrape_fn(url))
        except Exception as e:
            logger.warning("generic scrape raised for %s: %s: %s — skipping.", url, type(e).__name__, e)

    return discovery_results, generic_candidates


# --- main entry point --------------------------------------------------------------

def run_pipeline(
    gem_url: str,
    refresh: bool = False,
    *,
    extract_fn=gem_extractor.extract_gem_product,
    normalize_fn=llm_client.normalize_product,
    flipkart_search_fn=flipkart_scraper.search,
    amazon_search_fn=amazon_scraper.search,
    discover_fn=discovery.discover_for_queries,
    generic_scrape_fn=generic_scraper.scrape,
    match_fn=matcher.match_candidates,
    expand_search_fn=llm_client.expand_search,
) -> dict:
    if not refresh:
        cached = _cache_get(gem_url)
        if cached is not None:
            result = dict(cached)
            result["from_cache"] = True
            return result

    trace_id = uuid.uuid4().hex[:16]
    trace: dict = {
        "trace_id": trace_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gem_url": gem_url,
        "stages": {},
    }

    # --- stage 1: extract -----------------------------------------------------------
    gem_product = extract_fn(gem_url)
    gem_product_dict = gem_product.to_dict() if hasattr(gem_product, "to_dict") else gem_product
    trace["stages"]["extraction"] = gem_product_dict

    # --- stage 2: normalize ----------------------------------------------------------
    normalized = normalize_fn(gem_product_dict)
    trace["stages"]["normalization"] = normalized

    queries = normalized.get("search_queries") or [normalized.get("canonical_name", "") or gem_product_dict.get("title", "")]
    primary_query = queries[0]

    # --- stage 3/4 first pass: Flipkart + Amazon direct search -----------------------
    flipkart_result = flipkart_search_fn(primary_query)
    amazon_result = amazon_search_fn(primary_query)
    trace["stages"]["flipkart_search"] = flipkart_result.to_dict() if hasattr(flipkart_result, "to_dict") else flipkart_result
    trace["stages"]["amazon_search"] = amazon_result.to_dict() if hasattr(amazon_result, "to_dict") else amazon_result

    # --- stage 3/4 first pass: open discovery + generic scrape for everything else ---
    discovery_results, generic_candidates = _run_discovery_and_generic_scrape(
        queries, None, discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn
    )
    trace["stages"]["discovery"] = [d.to_dict() if hasattr(d, "to_dict") else d for d in discovery_results]
    trace["stages"]["generic_scrapes"] = [c.to_dict() if hasattr(c, "to_dict") else c for c in generic_candidates]

    all_candidates = list(flipkart_result.candidates) + list(amazon_result.candidates) + generic_candidates

    # --- stage 5: match ---------------------------------------------------------------
    match_result = match_fn(normalized, all_candidates)
    trace["stages"]["matching"] = match_result.to_dict() if hasattr(match_result, "to_dict") else match_result

    tried_domains = {"flipkart.com", "amazon.in"} | {_domain_of_candidate(c) for c in generic_candidates}
    confirmed = list(match_result.confirmed_matches)

    # --- retry: only if the first pass found nothing ----------------------------------
    retry_trace = {"triggered": False}
    if not confirmed:
        retry_trace["triggered"] = True
        expansion = expand_search_fn(normalized, list(queries), sorted(tried_domains))
        retry_trace["expansion"] = expansion

        retry_queries = expansion.get("new_queries") or []
        retry_domains = expansion.get("suggested_domains") or None  # None -> open discovery if nothing suggested

        if retry_queries:
            retry_discovery_results, retry_generic_candidates = _run_discovery_and_generic_scrape(
                retry_queries, retry_domains, discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn
            )
            retry_trace["discovery"] = [d.to_dict() if hasattr(d, "to_dict") else d for d in retry_discovery_results]
            retry_trace["generic_scrapes"] = [c.to_dict() if hasattr(c, "to_dict") else c for c in retry_generic_candidates]

            retry_match_result = match_fn(normalized, retry_generic_candidates)
            retry_trace["matching"] = retry_match_result.to_dict() if hasattr(retry_match_result, "to_dict") else retry_match_result

            confirmed = list(retry_match_result.confirmed_matches)
            tried_domains |= {_domain_of_candidate(c) for c in retry_generic_candidates}
        else:
            retry_trace["skipped_reason"] = "expand_search returned no new queries to try"

    trace["stages"]["retry"] = retry_trace

    matched_domains = {m["source_domain"] for m in confirmed}
    not_found_on = sorted(tried_domains - matched_domains)

    result = {
        "gem_product": gem_product_dict,
        "matches": confirmed,
        "not_found_on": not_found_on,
        "from_cache": False,
        "trace_id": trace_id,
    }

    trace["final_result"] = {"matches": confirmed, "not_found_on": not_found_on}
    _write_trace(trace)
    _cache_set(gem_url, result)

    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python pipeline.py <gem_url> [--refresh]")
        sys.exit(1)

    refresh = "--refresh" in sys.argv
    url = sys.argv[1]
    out = run_pipeline(url, refresh=refresh)
    print(json.dumps(out, indent=2, ensure_ascii=False))
