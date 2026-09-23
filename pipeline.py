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


# --- candidate helpers ----------------------------------------------------------

def _cand_dict(candidate) -> dict:
    return candidate.to_dict() if hasattr(candidate, "to_dict") else candidate


def _str_list(value) -> list[str]:
    """Coerce to a list of non-empty strings. LLM fields sometimes arrive as
    a bare string — queries[0] on a string is the first CHARACTER, and
    discovery would iterate per character. Never trust model types."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


def _usable(candidate) -> bool:
    """False for candidates whose fetch failed with no evidence at all —
    sending those to the LLM lets it 'judge' from the reference alone."""
    d = _cand_dict(candidate)
    if d.get("fetch_error") and not d.get("raw_page_text") and not d.get("structured_fields"):
        return False
    return True


def _dedup(candidates: list) -> list:
    """Cross-source URL dedup (the same Flipkart listing could arrive from
    the direct scraper AND from discovery) — duplicate verify calls and
    duplicate rows in the comparison otherwise."""
    seen: set[str] = set()
    out = []
    for c in candidates:
        d = _cand_dict(c)
        key = (d.get("url") or "").split("#")[0].rstrip("/")
        if not key:
            key = f"__noid_{len(out)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _record_search_health(result, tried_domains: set, search_issues: list) -> None:
    """A blocked/errored search is a SCRAPE FAILURE, not 'product not sold
    here' — previously both looked identical in not_found_on."""
    d = _cand_dict(result)
    domain = d.get("source_domain", "?")
    if d.get("blocked"):
        search_issues.append(f"{domain}: blocked/CAPTCHA — scrape failed (not counted as 'not found')")
    elif d.get("error"):
        search_issues.append(f"{domain}: search error: {d['error']}")
    else:
        tried_domains.add(domain)


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

    queries = _str_list(normalized.get("search_queries"))
    if not queries:
        fallback = (normalized.get("canonical_name") or gem_product_dict.get("title") or "").strip()
        queries = [fallback] if fallback else [gem_product_dict.get("category_slug") or "product"]
    primary_query = queries[0]

    # --- stage 3/4 first pass: Flipkart + Amazon direct search -----------------------
    flipkart_result = flipkart_search_fn(primary_query)
    amazon_result = amazon_search_fn(primary_query)
    trace["stages"]["flipkart_search"] = flipkart_result.to_dict() if hasattr(flipkart_result, "to_dict") else flipkart_result
    trace["stages"]["amazon_search"] = amazon_result.to_dict() if hasattr(amazon_result, "to_dict") else amazon_result

    # Search health decides who's honestly "searched" vs who failed to scrape.
    search_issues: list[str] = []
    tried_domains: set = set()
    _record_search_health(flipkart_result, tried_domains, search_issues)
    _record_search_health(amazon_result, tried_domains, search_issues)

    # --- stage 3/4 first pass: open discovery + generic scrape for everything else ---
    discovery_results, generic_candidates = _run_discovery_and_generic_scrape(
        queries, None, discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn
    )
    trace["stages"]["discovery"] = [d.to_dict() if hasattr(d, "to_dict") else d for d in discovery_results]
    trace["stages"]["generic_scrapes"] = [c.to_dict() if hasattr(c, "to_dict") else c for c in generic_candidates]

    for dr in discovery_results:
        d = dr.to_dict() if hasattr(dr, "to_dict") else dr
        if d.get("blocked"):
            search_issues.append(f"discovery blocked for query {d.get('query')!r}")
        elif d.get("error"):
            search_issues.append(f"discovery error for query {d.get('query')!r}: {d['error']}")
    for gc in generic_candidates:
        d = _cand_dict(gc)
        if d.get("fetch_error") and not d.get("raw_page_text") and not d.get("structured_fields"):
            search_issues.append(f"{d.get('source_domain', '?')}: fetch failed ({d['fetch_error']})")
        elif d.get("source_domain"):
            tried_domains.add(d["source_domain"])

    all_candidates = _dedup(
        [c for c in list(flipkart_result.candidates) + list(amazon_result.candidates) + generic_candidates if _usable(c)]
    )

    # --- stage 5: match ---------------------------------------------------------------
    match_result = match_fn(normalized, all_candidates)
    final_match_result = match_result
    trace["stages"]["matching"] = match_result.to_dict() if hasattr(match_result, "to_dict") else match_result

    confirmed = list(match_result.confirmed_matches)

    # --- retry: only if the first pass found nothing ----------------------------------
    retry_trace = {"triggered": False}
    if not confirmed:
        retry_trace["triggered"] = True
        # expand_search is OPTIONAL logic — if it throws (bad JSON, 429,
        # timeout) the run must survive on its first-pass results. It
        # previously propagated and destroyed a fully successful run.
        try:
            expansion = expand_search_fn(normalized, list(queries), sorted(tried_domains | set(KNOWN_MARKETPLACE_DOMAINS)))
        except Exception as e:
            retry_trace["expansion_error"] = f"{type(e).__name__}: {e}"
            logger.warning("expand_search failed (%s) — skipping retry, keeping first-pass results.", e)
            expansion = {"new_queries": [], "suggested_domains": [], "notes": ""}
        retry_trace["expansion"] = expansion

        retry_queries = _str_list(expansion.get("new_queries"))
        retry_domains = _str_list(expansion.get("suggested_domains")) or None  # None -> open discovery

        if retry_queries:
            # Re-search the two DIRECT scrapers with the expanded query —
            # expansion used to feed only the (dead) Google discovery path,
            # so a bad primary query on the working marketplaces was never
            # retried with the LLM's better phrasing.
            retry_flipkart = flipkart_search_fn(retry_queries[0])
            retry_amazon = amazon_search_fn(retry_queries[0])
            trace["stages"]["flipkart_search_retry"] = (
                retry_flipkart.to_dict() if hasattr(retry_flipkart, "to_dict") else retry_flipkart
            )
            trace["stages"]["amazon_search_retry"] = retry_amazon.to_dict() if hasattr(retry_amazon, "to_dict") else retry_amazon
            _record_search_health(retry_flipkart, tried_domains, search_issues)
            _record_search_health(retry_amazon, tried_domains, search_issues)

            retry_discovery_results, retry_generic_candidates = _run_discovery_and_generic_scrape(
                retry_queries, retry_domains, discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn
            )
            retry_trace["discovery"] = [d.to_dict() if hasattr(d, "to_dict") else d for d in retry_discovery_results]
            retry_trace["generic_scrapes"] = [c.to_dict() if hasattr(c, "to_dict") else c for c in retry_generic_candidates]
            for gc in retry_generic_candidates:
                d = _cand_dict(gc)
                if d.get("fetch_error") and not d.get("raw_page_text") and not d.get("structured_fields"):
                    search_issues.append(f"{d.get('source_domain', '?')}: fetch failed on retry ({d['fetch_error']})")
                elif d.get("source_domain"):
                    tried_domains.add(d["source_domain"])

            # Re-match EVERYTHING still in play: first-pass candidates
            # (including any skipped by a transient LLM failure) plus the
            # retry's new ones — previously retry discarded pass-1 candidates
            # entirely.
            retry_pool = _dedup(
                [c for c in list(all_candidates) + list(retry_flipkart.candidates) + list(retry_amazon.candidates)
                 + retry_generic_candidates if _usable(c)]
            )
            retry_match_result = match_fn(normalized, retry_pool)
            final_match_result = retry_match_result
            retry_trace["matching"] = retry_match_result.to_dict() if hasattr(retry_match_result, "to_dict") else retry_match_result

            confirmed = list(retry_match_result.confirmed_matches)
        else:
            retry_trace["skipped_reason"] = "expand_search returned no new queries to try"

    trace["stages"]["retry"] = retry_trace

    matched_domains = {m.get("source_domain") for m in confirmed}
    not_found_on = sorted(tried_domains - matched_domains)

    # --- comparison arithmetic — the actual point of the project ----------------------
    gem_price = gem_product_dict.get("gem_price")
    for m in confirmed:
        price = m.get("price_used")
        if gem_price is not None and price is not None:
            m["delta_vs_gem"] = price - gem_price
            m["cheaper_than_gem"] = price < gem_price
    priced = [m["price_used"] for m in confirmed if m.get("price_used") is not None]
    comparison = {
        "gem_price": gem_price,
        "gem_price_type": gem_product_dict.get("gem_price_type", "unknown"),
        "cheapest_match_price": min(priced) if priced else None,
        "savings_vs_gem": (gem_price - min(priced)) if (gem_price is not None and priced) else None,
    }

    skipped = list(final_match_result.skipped) if hasattr(final_match_result, "skipped") else []

    result = {
        "gem_product": gem_product_dict,
        "matches": confirmed,
        "not_found_on": not_found_on,
        "search_issues": search_issues,
        "skipped": skipped,
        "comparison": comparison,
        "from_cache": False,
        "trace_id": trace_id,
    }

    trace["final_result"] = {"matches": confirmed, "not_found_on": not_found_on, "search_issues": search_issues}
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
