"""
Part 6 — orchestrator.

run_pipeline(gem_url) is the one function everything else in this project
exists to support: GeM URL in, price comparison out. Sequence:

  extract (1) -> normalize (2) -> [flipkart.search + amazon.search (4)] and
  [discover (3) -> generic.scrape (4)] -> match (5) -> if zero confirmed
  matches: expand_search (2) -> retry direct searches + discovery + generic
  once, re-match -> cache + trace -> return.

Every external collaborator (extract_fn, normalize_fn, the two search
functions, discover_fn, generic_scrape_fn, match_fn, expand_search_fn) is
an overridable keyword argument defaulting to the real implementation.
That's not incidental — it's what makes this module's own orchestration
logic (sequencing, the retry trigger, cache, trace shape, not_found_on
computation) testable in isolation from whether the real scrapers/LLM
calls work, the same way the previous parts were tested independently.

Scope note on the retry step: expand_search's new_queries feed BOTH the
direct Flipkart/Amazon scrapers (not just discovery — a bad primary
phrasing used to never get the LLM's own alternate tried on the working
marketplaces) and discovery+generic for sites those scrapers don't cover.

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
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from config import CACHE_DIR, CACHE_TTL_SECONDS, TRACES_DIR, KNOWN_MARKETPLACE_DOMAINS, logger
import gem_extractor
import llm_client
import discovery
import matcher
from scrapers import flipkart as flipkart_scraper
from scrapers import amazon as amazon_scraper
from scrapers import generic as generic_scraper

MAX_GENERIC_CANDIDATES = 8  # cap how many brand-site/discovery URLs get scraped per run
# A direct search is "enough" once it has this many candidates — stop trying
# further normalized queries so a 3-query normalize doesn't cost 3 Selenium
# runs when the first query already returned a full page of results.
DIRECT_SEARCH_MIN_CANDIDATES = 5
# Trace files are ~100-200KB each and grow forever otherwise.
TRACE_MAX_FILES = 200


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
        # tmp + os.replace: a crash mid-write otherwise leaves a truncated
        # JSON that every later read has to discard as a miss anyway.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({"cached_at": time.time(), "result": result}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
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
    # Rotate: keep the newest TRACE_MAX_FILES traces, delete the rest.
    # Unbounded growth was ~100-200KB per live run forever.
    try:
        traces = sorted(TRACES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in traces[TRACE_MAX_FILES:]:
            old.unlink(missing_ok=True)
    except OSError as e:
        logger.debug("Trace rotation skipped: %s", e)
    return trace_id


# --- helpers ------------------------------------------------------------------------

def _search_direct(search_fn, queries: list[str], *, min_candidates: int = DIRECT_SEARCH_MIN_CANDIDATES):
    """Run a direct marketplace search over normalized queries until one
    returns >= min_candidates (or queries run out).

    Previously only queries[0] ever reached Amazon/Flipkart — secondary
    normalized queries fed discovery only, and the retry used
    expand_search's new_queries[0], so a bad primary phrasing meant the
    two working marketplaces were searched once, wrong, and never retried
    with the LLM's own alternate phrasing. The early-exit keeps a 3-query
    normalize from costing 3 Selenium runs when query 1 already returned
    a full page.

    Returns (results, all_candidates): one SearchScrapeResult per query
    actually run, and the cross-query URL-deduped candidate list."""
    results = []
    seen: set[str] = set()
    all_candidates = []
    for q in queries:
        r = search_fn(q)
        results.append(r)
        for c in getattr(r, "candidates", None) or []:
            d = _cand_dict(c)
            key = (d.get("url") or "").split("#")[0].rstrip("/")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            all_candidates.append(c)
        if len(all_candidates) >= min_candidates:
            break
    return results, all_candidates


def _run_discovery_and_generic_scrape(
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
    lanes = [list(d.get("filtered_urls", [])) for d in dicts]
    urls: list[str] = []
    while any(lanes) and len(urls) < MAX_GENERIC_CANDIDATES:
        for lane in lanes:
            if not lane or len(urls) >= MAX_GENERIC_CANDIDATES:
                continue
            u = lane.pop(0)
            if u not in urls:
                urls.append(u)

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

    # --- stage 3/4 first pass: Flipkart + Amazon direct search -----------------------
    # Every normalized query is eligible (early-exit once one query has
    # enough candidates), not just queries[0].
    flipkart_results, flipkart_candidates = _search_direct(flipkart_search_fn, queries)
    amazon_results, amazon_candidates = _search_direct(amazon_search_fn, queries)
    trace["stages"]["flipkart_search"] = [r.to_dict() if hasattr(r, "to_dict") else r for r in flipkart_results]
    trace["stages"]["amazon_search"] = [r.to_dict() if hasattr(r, "to_dict") else r for r in amazon_results]

    # Search health decides who's honestly "searched" vs who failed to scrape.
    search_issues: list[str] = []
    tried_domains: set = set()
    for r in flipkart_results + amazon_results:
        _record_search_health(r, tried_domains, search_issues)

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
        [c for c in list(flipkart_candidates) + list(amazon_candidates) + generic_candidates if _usable(c)]
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
        # Strict-mode Indian filter: the prompt asks for Indian domains but
        # doesn't enforce it — a foreign suggestion (bestbuy.com) must not
        # reopen open-mode .com discovery.
        retry_domains = [d for d in _str_list(expansion.get("suggested_domains")) if discovery._is_indian_retail(d)] or None  # None -> open discovery

        if retry_queries:
            # Re-search the two DIRECT scrapers with the expanded queries —
            # expansion used to feed only the (dead) Google discovery path,
            # so a bad primary query on the working marketplaces was never
            # retried with the LLM's better phrasing. All retry_queries are
            # eligible, not just [0].
            retry_flipkart_results, retry_flipkart_candidates = _search_direct(flipkart_search_fn, retry_queries)
            retry_amazon_results, retry_amazon_candidates = _search_direct(amazon_search_fn, retry_queries)
            trace["stages"]["flipkart_search_retry"] = [
                r.to_dict() if hasattr(r, "to_dict") else r for r in retry_flipkart_results
            ]
            trace["stages"]["amazon_search_retry"] = [
                r.to_dict() if hasattr(r, "to_dict") else r for r in retry_amazon_results
            ]
            for r in retry_flipkart_results + retry_amazon_results:
                _record_search_health(r, tried_domains, search_issues)

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
                [c for c in list(all_candidates) + list(retry_flipkart_candidates) + list(retry_amazon_candidates)
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

    # not_found_on is a verdict ("we searched cleanly, nothing matched") —
    # a domain whose candidates were all SKIPPED (verify never returned a
    # decision: LLM outage, null-price demotion) never got that verdict, so
    # it must not appear here. Report it as a search issue instead.
    skipped = list(final_match_result.skipped) if hasattr(final_match_result, "skipped") else []
    skipped_domains: set = set()
    for u in skipped:
        if isinstance(u, str) and u.startswith("http"):
            d = urlparse(u).netloc.lower().removeprefix("www.")
            if d:
                skipped_domains.add(d)
    verify_incomplete = sorted((tried_domains - matched_domains) & skipped_domains)
    not_found_on = sorted(tried_domains - matched_domains - skipped_domains)
    for d in verify_incomplete:
        search_issues.append(f"{d}: candidates skipped (verify incomplete) — not counted as 'not found'")

    # Dedupe while preserving order — retry paths can append the same
    # domain's issue twice.
    seen_issues: set = set()
    search_issues = [i for i in search_issues if not (i in seen_issues or seen_issues.add(i))]

    # --- comparison arithmetic — the actual point of the project ----------------------
    gem_price = gem_product_dict.get("gem_price")
    warnings: list[str] = []
    if gem_price is None:
        warnings.append(
            "GeM price not extracted — price-sanity gate is OFF and savings comparison unavailable."
        )
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

    result = {
        "gem_product": gem_product_dict,
        "matches": confirmed,
        "not_found_on": not_found_on,
        "search_issues": search_issues,
        "skipped": skipped,
        "warnings": warnings,
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
