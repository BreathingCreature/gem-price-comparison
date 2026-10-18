"""
Part 6 — orchestrator.

run_pipeline(gem_url) is the one function everything else in this project
exists to support: GeM URL in, price comparison out. Sequence:

  extract (1) -> normalize (2) -> [flipkart.search + amazon.search (4)] and
  [discover (3) -> generic.scrape (4)] -> match (5) -> if zero confirmed
  matches: expand_search (2) -> retry direct searches + discovery + generic
  once, re-match -> cache + trace -> return.

  With GEM_PARALLEL=1 the first pass becomes three lanes — Flipkart, Amazon
  and discovery+generic — running at the same time, each checking its own
  listings as soon as its search returns; the lanes' match batches are then
  merged back into one (first copy of a listing wins, cheapest first).

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
from functools import partial
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from core import events
from core import prefilter
from core.config import CACHE_DIR, CACHE_TTL_SECONDS, TRACE_MAX_FILES, TRACES_DIR, KNOWN_MARKETPLACE_DOMAINS, logger
from core.config import PARALLEL_SEARCH
from core.config import DISCOVERY_ENABLED, PREFILTER_TOP, REUSE_VERDICTS
from core.parallel import run_many, run_pair
from core.config import RETRY_ENABLED
from scrapers import gem as gem_extractor
from llm import client as llm_client
import discovery
from llm import match as matcher
from scrapers import flipkart as flipkart_scraper
from scrapers import amazon as amazon_scraper
from scrapers import generic as generic_scraper
import re

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


def _brief(candidate) -> dict:
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


def _emit_finished(result: dict) -> None:
    comparison = result.get("comparison") or {}
    events.emit(
        "run_finished",
        from_cache=bool(result.get("from_cache")),
        matches=len(result.get("matches") or []),
        cheapest=comparison.get("cheapest_match_price"),
        savings=comparison.get("savings_vs_gem"),
        trace_id=result.get("trace_id"),
    )


def _usable(candidate) -> bool:
    """False for candidates whose fetch failed with no evidence at all —
    sending those to the LLM lets it 'judge' from the reference alone."""
    d = _cand_dict(candidate)
    if d.get("fetch_error") and not d.get("raw_page_text") and not d.get("structured_fields"):
        return False
    return True


def _listing_key(url: str) -> str:
    """Per-site key for dedup: ASIN for Amazon, pid/itm for Flipkart,
    cleaned URL for everything else.

    For other shops: lowercase host, drop fragment, drop trailing slash,
    drop tracking parameters (utm_*, gclid, fbclid, ref, ref_, srsltid,
    spm, cmpid, affid), keep all other parameters sorted by name.
    """
    url = (url or "").strip()
    parsed = urlparse(url if "//" in url else "//" + url)
    host = (parsed.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = (parsed.path or "").rstrip("/")

    if host.endswith("amazon.in") or host.endswith("amazon.com"):
        m = re.search(r"/(?:dp|gp/product|product)/([A-Za-z0-9]{10})", path)
        if m:
            return "asin:" + m.group(1).upper()
    if host.endswith("flipkart.com"):
        pid = parse_qs(parsed.query).get("pid")
        if pid:
            return "pid:" + pid[0].upper()
        m = re.search(r"/p/(itm[a-z0-9]+)", path, re.I)
        if m:
            return "itm:" + m.group(1).lower()

    # Other shops: host + path + sorted non-tracking query params
    tracking = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                "gclid", "fbclid", "ref", "ref_", "srsltid", "spm", "cmpid", "affid"}
    params = parse_qs(parsed.query)
    kept = sorted((k, v[0] if v else "") for k, v in params.items() if k not in tracking)
    qs = "&".join(f"{k}={v}" for k, v in kept)
    return f"{host}{path}?{qs}" if qs else f"{host}{path}"


def _dedup(candidates: list) -> list:
    """Cross-source URL dedup (the same Flipkart listing could arrive from
    the direct scraper AND from discovery) — duplicate verify calls and
    duplicate rows in the comparison otherwise."""
    seen: set[str] = set()
    out = []
    for c in candidates:
        d = _cand_dict(c)
        key = _listing_key(d.get("url"))
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

def _search_direct(search_fn, queries: list[str], *, min_candidates: int = DIRECT_SEARCH_MIN_CANDIDATES, label: str = ""):
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
            candidates=[_brief(c) for c in found][:20],
        )
        for c in getattr(r, "candidates", None) or []:
            d = _cand_dict(c)
            key = _listing_key(d.get("url"))
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            all_candidates.append(c)
        if len(all_candidates) >= min_candidates:
            break
    return results, all_candidates


def _search_marketplaces(flipkart_search_fn, amazon_search_fn, queries: list[str], *, again: bool = False):
    """Search Flipkart and Amazon. With GEM_PARALLEL=1 both run at the same
    time, otherwise one after the other (Flipkart first), as before.
    Returns (flipkart_results, flipkart_candidates, amazon_results, amazon_candidates)."""
    kind = "retry" if again else "search"
    suffix = " again" if again else ""
    if PARALLEL_SEARCH:
        events.emit("stage", name=f"{kind}_both", label=f"Searching Flipkart and Amazon at the same time{suffix}")
        (f_results, f_candidates), (a_results, a_candidates) = run_pair(
            lambda: _search_direct(flipkart_search_fn, queries, label="flipkart"),
            lambda: _search_direct(amazon_search_fn, queries, label="amazon"),
            parallel=True,
        )
    else:
        events.emit("stage", name=f"{kind}_flipkart", label=f"Searching Flipkart{suffix}")
        f_results, f_candidates = _search_direct(flipkart_search_fn, queries, label="flipkart")
        events.emit("stage", name=f"{kind}_amazon", label=f"Searching Amazon{suffix}")
        a_results, a_candidates = _search_direct(amazon_search_fn, queries, label="amazon")
    return f_results, f_candidates, a_results, a_candidates


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

    events.emit("discovery_finished", queries=list(queries), urls=urls)

    def _scrape_one(url: str):
        events.emit("scrape_started", url=url)
        try:
            scraped = generic_scrape_fn(url)
        except Exception as e:
            logger.warning("generic scrape raised for %s: %s: %s — skipping.", url, type(e).__name__, e)
            return None
        events.emit("scrape_finished", **_brief(scraped))
        return scraped

    # Plain HTTP, no browser — the (up to 8) brand-site pages are fetched at
    # the same time instead of one after another. One bad page still only
    # skips itself.
    jobs = [partial(_scrape_one, u) for u in urls]
    scraped_pages = run_many(jobs, workers=len(jobs)) if jobs else []
    generic_candidates = [page for page in scraped_pages if page is not None]

    return discovery_results, generic_candidates


def _prefilter_candidates(candidates: list, normalized) -> tuple[list, list]:
    """GEM_PREFILTER_TOP: keep only the best N titled listings per shop before
    spending LLM calls. Off (0, the default) returns the input untouched."""
    if PREFILTER_TOP <= 0:
        return candidates, []
    return prefilter.filter_candidates(candidates, normalized, PREFILTER_TOP)


def _verify_lane(normalized, candidates: list, match_fn, *, name: str, label: str):
    """One lane's own listings, checked the moment they arrive (GEM_PARALLEL=1).
    The prefilter trims weak titles per shop first. An empty lane never
    reaches the matcher — there is nothing to check. Returns
    (match_result, dropped_by_prefilter)."""
    usable = [c for c in candidates if _usable(c)]
    kept, dropped = _prefilter_candidates(usable, normalized)
    if not kept:
        return matcher.MatchBatchResult(), dropped
    events.emit("stage", name=f"match_{name}", label=f"Checking {label}")
    events.emit("candidates_total", count=len(kept))
    return match_fn(normalized, kept), dropped


def _run_first_pass_lanes(
    flipkart_search_fn,
    amazon_search_fn,
    queries: list[str],
    *,
    discover_fn,
    generic_scrape_fn,
    normalized,
    match_fn,
) -> list:
    """GEM_PARALLEL=1 first pass: three lanes — Flipkart, Amazon and
    discovery+generic — run at the same time, and each lane verifies its own
    candidates as soon as its search returns, so the LLM work of a fast lane
    overlaps the slow Flipkart lane instead of waiting for every search to
    finish. Returns one (results, candidates, match_result) tuple per lane,
    in that order."""
    events.emit("stage", name="search_both", label="Searching Flipkart, Amazon and other shops at the same time")
    if DISCOVERY_ENABLED:
        events.emit("stage", name="discovery", label="Looking for other shops")

    def flipkart_lane():
        results, candidates = _search_direct(flipkart_search_fn, queries, label="flipkart")
        lane_match, dropped = _verify_lane(normalized, candidates, match_fn, name="flipkart", label="the Flipkart listings")
        return results, candidates, lane_match, dropped

    def amazon_lane():
        results, candidates = _search_direct(amazon_search_fn, queries, label="amazon")
        lane_match, dropped = _verify_lane(normalized, candidates, match_fn, name="amazon", label="the Amazon listings")
        return results, candidates, lane_match, dropped

    def other_shops_lane():
        if not DISCOVERY_ENABLED:
            return [], [], matcher.MatchBatchResult(), []
        results, candidates = _run_discovery_and_generic_scrape(
            queries, None, discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn
        )
        lane_match, dropped = _verify_lane(normalized, candidates, match_fn, name="other_shops", label="other shops' listings")
        return results, candidates, lane_match, dropped

    return run_many([flipkart_lane, amazon_lane, other_shops_lane], workers=3)


def _merge_match_results(lane_results: list):
    """Merge the per-lane match batches (GEM_PARALLEL=1). The same listing can
    be checked by two lanes (a Flipkart URL arriving from both the direct
    search and discovery), so decisions are deduped by _listing_key — the
    first lane's copy wins. Confirmed matches are re-sorted cheapest first,
    like the matcher does within one batch."""
    kept: list = []
    kept_ids: set = set()
    seen_keys: set = set()
    skipped: list[str] = []
    seen_skipped: set = set()
    for lane in lane_results:
        decisions = lane.get("all_decisions") if isinstance(lane, dict) else getattr(lane, "all_decisions", None)
        for d in decisions or []:
            key = _listing_key(d.get("candidate_url")) or f"__noid_{len(kept)}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            kept.append(d)
            kept_ids.add(id(d))
        lane_skipped = lane.get("skipped") if isinstance(lane, dict) else getattr(lane, "skipped", None)
        for u in lane_skipped or []:
            key = _listing_key(u) or f"__noid_skip_{len(skipped)}"
            if key in seen_skipped:
                continue
            seen_skipped.add(key)
            skipped.append(u)

    confirmed: list = []
    for lane in lane_results:
        matches = lane.get("confirmed_matches") if isinstance(lane, dict) else getattr(lane, "confirmed_matches", None)
        # A lane's confirmed entry is the SAME dict object as its all_decisions
        # entry — a duplicate dropped above must not reappear here.
        for d in matches or []:
            if id(d) in kept_ids:
                confirmed.append(d)
    confirmed.sort(key=lambda d: d.get("price_used") if d.get("price_used") is not None else float("inf"))
    return matcher.MatchBatchResult(all_decisions=kept, confirmed_matches=confirmed, skipped=skipped)


# --- main entry point --------------------------------------------------------------

def _run_pipeline(
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
    events.emit("run_started", gem_url=gem_url, refresh=refresh)
    if not refresh:
        cached = _cache_get(gem_url)
        if cached is not None:
            result = dict(cached)
            result["from_cache"] = True
            events.emit("cache_hit", gem_url=gem_url)
            _emit_finished(result)
            return result

    trace_id = uuid.uuid4().hex[:16]
    trace: dict = {
        "trace_id": trace_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gem_url": gem_url,
        "stages": {},
    }

    try:
        # --- stage 1: extract -----------------------------------------------------------
        events.emit("stage", name="extract", label="Reading the GeM product page")
        gem_product = extract_fn(gem_url)
        gem_product_dict = gem_product.to_dict() if hasattr(gem_product, "to_dict") else gem_product
        trace["stages"]["extraction"] = gem_product_dict
        events.emit(
            "gem_product",
            title=gem_product_dict.get("title"),
            price=gem_product_dict.get("gem_price"),
            price_type=gem_product_dict.get("gem_price_type"),
            brand=gem_product_dict.get("brand"),
            model=gem_product_dict.get("model"),
            category=gem_product_dict.get("category_slug"),
            specs=gem_product_dict.get("specs") or {},
        )

        # --- stage 2: normalize ----------------------------------------------------------
        events.emit("stage", name="normalize", label="Working out what the product really is")
        normalized = normalize_fn(gem_product_dict)
        trace["stages"]["normalization"] = normalized
        events.emit(
            "normalized",
            canonical_name=normalized.get("canonical_name"),
            brand=normalized.get("brand"),
            model=normalized.get("model"),
            queries=_str_list(normalized.get("search_queries")),
            key_identifiers=_str_list(normalized.get("key_identifiers")),
        )

        queries = _str_list(normalized.get("search_queries"))
        if not queries:
            fallback = (normalized.get("canonical_name") or gem_product_dict.get("title") or "").strip()
            queries = [fallback] if fallback else [gem_product_dict.get("category_slug") or "product"]

        # --- stage 3/4 first pass: Flipkart + Amazon direct search -----------------------
        # Every normalized query is eligible (early-exit once one query has
        # enough candidates), not just queries[0].
        prefilter_dropped: list = []
        if PARALLEL_SEARCH:
            # GEM_PARALLEL=1: three lanes — Flipkart, Amazon and discovery+generic
            # — search at the same time, each checking its own listings the moment
            # they arrive (so a fast lane's LLM work overlaps the slow Flipkart lane).
            (
                (flipkart_results, flipkart_candidates, flipkart_match, flipkart_dropped),
                (amazon_results, amazon_candidates, amazon_match, amazon_dropped),
                (discovery_results, generic_candidates, other_shops_match, other_shops_dropped),
            ) = _run_first_pass_lanes(
                flipkart_search_fn, amazon_search_fn, queries,
                discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn,
                normalized=normalized, match_fn=match_fn,
            )
            prefilter_dropped = list(flipkart_dropped) + list(amazon_dropped) + list(other_shops_dropped)
            if PREFILTER_TOP > 0:
                trace["stages"]["prefilter"] = {"top": PREFILTER_TOP, "dropped": prefilter_dropped}
        else:
            flipkart_results, flipkart_candidates, amazon_results, amazon_candidates = _search_marketplaces(
                flipkart_search_fn, amazon_search_fn, queries
            )
            discovery_results = generic_candidates = None
        trace["stages"]["flipkart_search"] = [r.to_dict() if hasattr(r, "to_dict") else r for r in flipkart_results]
        trace["stages"]["amazon_search"] = [r.to_dict() if hasattr(r, "to_dict") else r for r in amazon_results]

        # Search health decides who's honestly "searched" vs who failed to scrape.
        search_issues: list[str] = []
        tried_domains: set = set()
        for r in flipkart_results + amazon_results:
            _record_search_health(r, tried_domains, search_issues)

        # --- stage 3/4 first pass: open discovery + generic scrape for everything else ---
        if not PARALLEL_SEARCH:
            if DISCOVERY_ENABLED:
                events.emit("stage", name="discovery", label="Looking for other shops")
                discovery_results, generic_candidates = _run_discovery_and_generic_scrape(
                    queries, None, discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn
                )
            else:
                discovery_results, generic_candidates = [], []
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
        if PARALLEL_SEARCH:
            # Each lane already checked its own listings (and emitted its own
            # match stage); merging keeps one row per listing, first lane wins.
            match_result = _merge_match_results([flipkart_match, amazon_match, other_shops_match])
        else:
            events.emit("stage", name="match", label="Checking which listings are the same product")
            match_input, dropped_now = _prefilter_candidates(all_candidates, normalized)
            prefilter_dropped.extend(dropped_now)
            events.emit("candidates_total", count=len(match_input))
            match_result = match_fn(normalized, match_input)
            if PREFILTER_TOP > 0:
                trace["stages"]["prefilter"] = {"top": PREFILTER_TOP, "dropped": prefilter_dropped}
        final_match_result = match_result
        trace["stages"]["matching"] = match_result.to_dict() if hasattr(match_result, "to_dict") else match_result

        confirmed = list(match_result.confirmed_matches)

        # --- retry: only if the first pass found nothing ----------------------------------
        retry_trace = {"triggered": False}
        if not confirmed and RETRY_ENABLED:
            retry_trace["triggered"] = True
            events.emit("stage", name="retry", label="Nothing matched yet, asking the LLM for better search words")
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
            events.emit("retry_plan", new_queries=_str_list(expansion.get("new_queries")), notes=expansion.get("notes", ""))

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
                (
                    retry_flipkart_results,
                    retry_flipkart_candidates,
                    retry_amazon_results,
                    retry_amazon_candidates,
                ) = _search_marketplaces(flipkart_search_fn, amazon_search_fn, retry_queries, again=True)
                trace["stages"]["flipkart_search_retry"] = [
                    r.to_dict() if hasattr(r, "to_dict") else r for r in retry_flipkart_results
                ]
                trace["stages"]["amazon_search_retry"] = [
                    r.to_dict() if hasattr(r, "to_dict") else r for r in retry_amazon_results
                ]
                for r in retry_flipkart_results + retry_amazon_results:
                    _record_search_health(r, tried_domains, search_issues)

                if DISCOVERY_ENABLED:
                    retry_discovery_results, retry_generic_candidates = _run_discovery_and_generic_scrape(
                        retry_queries, retry_domains, discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn
                    )
                else:
                    retry_discovery_results, retry_generic_candidates = [], []
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
                retry_pool, dropped_now = _prefilter_candidates(retry_pool, normalized)
                prefilter_dropped.extend(dropped_now)
                if PREFILTER_TOP > 0:
                    trace["stages"]["prefilter"] = {"top": PREFILTER_TOP, "dropped": prefilter_dropped}

                # GEM_REUSE_VERDICTS: the first pass already asked the LLM about
                # these listings and the answers will not change — only ask about
                # what is genuinely new. A listing whose first check was SKIPPED
                # never got a verdict, so it stays in the pool and is asked again.
                reused_decisions: list = []
                if REUSE_VERDICTS:
                    reused_decisions = list(match_result.all_decisions) if hasattr(match_result, "all_decisions") else []
                    decided_keys = {_listing_key(d.get("candidate_url")) for d in reused_decisions}
                    retry_pool = [c for c in retry_pool if _listing_key(_cand_dict(c).get("url")) not in decided_keys]
                    retry_trace["reused_verdicts"] = len(reused_decisions)

                events.emit("stage", name="match_retry", label="Checking the new listings")
                retry_match_result = match_fn(normalized, retry_pool)
                if reused_decisions:
                    # final all_decisions must still show what happened with the
                    # reused listings — only the confirmed/skipped come from the
                    # retry itself.
                    retry_match_result = matcher.MatchBatchResult(
                        all_decisions=reused_decisions + list(retry_match_result.all_decisions),
                        confirmed_matches=list(retry_match_result.confirmed_matches),
                        skipped=list(retry_match_result.skipped),
                    )
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
        priced = [m.get("price_used") for m in confirmed if m.get("price_used") is not None]
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
        _emit_finished(result)
        trace["events"] = events.snapshot()
        _write_trace(trace)
        if search_issues:
            logger.info("Not caching this run: %d search issue(s) reported.", len(search_issues))
        else:
            _cache_set(gem_url, result)

        return result
    except Exception as exc:
        # Any failure after the trace dict exists: write a partial trace first,
        # then re-raise the same exception so callers still see the error.
        trace["error"] = f"{type(exc).__name__}: {exc}"
        trace["events"] = events.snapshot()
        _write_trace(trace)
        raise


def run_pipeline(gem_url: str, refresh: bool = False, *, on_event=None, **overrides) -> dict:
    """Run the whole comparison for one GeM URL.

    on_event, if given, is called with one small dict for every progress
    event (see core/events.py and docs/api.md). The keyword overrides are
    the same stage functions as before and exist for tests."""
    with events.capture(on_event):
        return _run_pipeline(gem_url, refresh, **overrides)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python pipeline.py <gem_url> [--refresh]")
        sys.exit(1)

    refresh = "--refresh" in sys.argv
    url = sys.argv[1]
    out = run_pipeline(url, refresh=refresh)
    print(json.dumps(out, indent=2, ensure_ascii=False))
