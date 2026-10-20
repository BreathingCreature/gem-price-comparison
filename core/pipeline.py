"""
The orchestrator: GeM URL in, price comparison out.

run_pipeline(gem_url) is the one function everything else in this project
exists to support: GeM URL in, price comparison out. Sequence:

  extract (1) -> normalize (2) -> [flipkart.search + amazon.search (4)] and
  [discover (3) -> generic.scrape (4)] -> match (5) -> first result (state
  "shown", event run_finished) -> for the shops whose card says retryable:
  expand_search (2) -> re-search that shop once -> merge -> similar
  products (6.5) if still nothing -> final result (state "done", event done)
  -> cache + trace -> return.

  With GEM_PARALLEL=1 (the default) the first pass is three lanes - Flipkart,
  Amazon and discovery+generic - running at the same time, each checking its
  own listings as soon as its search returns; the lanes' match batches are
  then merged back into one (first copy of a listing wins, cheapest first).
  GEM_PARALLEL=0 keeps the old one-search-at-a-time path for tests and strict
  debugging.

Every external collaborator (extract_fn, normalize_fn, the two search
functions, discover_fn, generic_scrape_fn, match_fn, expand_search_fn) is
an overridable keyword argument defaulting to the real implementation;
flipkart_search_fn's default is None because the pipeline itself builds the
run's one-Chrome browser and injects its search method (any injected search
function takes over and no browser is started).
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
reasons) via the to_dict() every input object already has. It does NOT
capture the raw LLM prompt/response text — doing that would mean
reworking the already-tested LLM functions to expose it, which felt like
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
from urllib.parse import urlparse

from core import events
from core import prefilter
from core import result as result_module
from core import retry as retry_module
from core import runs as runs_store
from core import similar as similar_module
from core.config import (
    CACHE_DIR,
    CACHE_TTL_SECONDS,
    DISCOVERY_ENABLED,
    KNOWN_MARKETPLACE_DOMAINS,
    LANE_TIME_LIMIT_SECONDS,
    MATCH_CONFIDENCE_THRESHOLD,
    PARALLEL_SEARCH,
    PREFILTER_TOP,
    PRICE_SANITY_HIGH_RATIO,
    PRICE_SANITY_LOW_RATIO,
    RESULT_VERSION,
    RETRY_ENABLED,
    REUSE_VERDICTS,
    STRICT_MODE,
    TRACE_MAX_AGE_DAYS,
    TRACE_MAX_FILES,
    TRACES_DIR,
    logger,
)
from core.errors import redact_obj
from core.parallel import run_many
from llm import router as llm_router
from llm.router import GatewayExhausted
from scrapers import gem as gem_extractor
from llm import client as llm_client
import discovery
from llm import match as matcher
from scrapers import flipkart as flipkart_scraper
from scrapers import amazon as amazon_scraper
from scrapers import generic as generic_scraper

# A direct search is "enough" once it has this many RELEVANT candidates —
# stop trying further normalized queries so a 3-query normalize doesn't cost
# 3 Selenium runs when the first query already returned a full page of
# results, while junk from query 1 no longer ends the loop (C7).
DIRECT_SEARCH_MIN_CANDIDATES = 5
# E7: the rotation scan stats every trace file; running it every 25th write
# keeps the folder within TRACE_MAX_FILES + 24 without paying that per run.
TRACE_ROTATE_EVERY = 25


# --- candidate helpers ----------------------------------------------------------
# The pure ones live in core/candidates.py; the names stay importable here
# because tests and the older tools call pipeline._dedup etc. directly.

from core.candidates import cand_dict as _cand_dict  # noqa: E402
from core.candidates import dedup as _dedup  # noqa: E402
from core.candidates import listing_identity as _listing_key  # noqa: E402
from core.candidates import usable as _usable  # noqa: E402
from core.searches import (  # noqa: E402
    DIRECT_SEARCH_MIN_CANDIDATES,
    MAX_GENERIC_CANDIDATES,
    brief as _brief,
    only_blocks as _only_blocks,
    record_search_health as _record_search_health,
    run_discovery_and_generic_scrape as _run_discovery_and_generic_scrape,
    search_direct as _search_direct,
    search_marketplaces as _search_marketplaces_impl,
)


def _str_list(value) -> list[str]:
    """Coerce to a list of non-empty strings. LLM fields sometimes arrive as
    a bare string — queries[0] on a string is the first CHARACTER, and
    discovery would iterate per character. Never trust model types."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


def _emit_finished(result: dict) -> None:
    comparison = result.get("comparison") or {}
    events.emit(
        "run_finished",
        from_cache=bool(result.get("from_cache")),
        matches=len(result.get("matches") or []),
        cheapest=comparison.get("cheapest_match_price"),
        savings=comparison.get("savings_vs_gem"),
        trace_id=result.get("trace_id"),
        # The first honest result travels with the event: the CLI prints its
        # first block from here, and the page can show it without a second
        # request. The same object is also in the run record on disk.
        result=result,
    )


def _identity_without_llm(gem_product_dict: dict) -> dict:
    """The run must still search when the LLM is out of allowance: the GeM
    title is a worse identity than the normalized one, but it is honest."""
    title = (gem_product_dict.get("title") or "").strip()
    return {
        "canonical_name": title,
        "brand": gem_product_dict.get("brand"),
        "model": gem_product_dict.get("model"),
        "key_identifiers": [],
        "search_queries": [title] if title else [],
        "category_slug": gem_product_dict.get("category_slug", ""),
        "specs": gem_product_dict.get("specs") or {},
        "gem_price": gem_product_dict.get("gem_price"),
        "gem_price_type": gem_product_dict.get("gem_price_type", "unknown"),
    }


def _note_llm_offline(warnings: list, message: str) -> None:
    """One warning per distinct problem; a lane-per-lane outage should not
    repeat the same sentence three times."""
    if message not in warnings:
        warnings.append(message)
    events.emit("llm_unavailable", reason=message)


def _guarded_match(match_fn, warnings: list):
    """A match batch whose LLM is unavailable must not kill the run: every
    listing in that batch is reported as skipped (the honest 'not checked')
    and one warning says why. Busy means 'retry later' and used-up means
    'not today' — both are the same to the current report."""

    def guarded(normalized, candidates, **kwargs):
        try:
            return match_fn(normalized, candidates, **kwargs)
        except llm_router.GatewayError as exc:
            _note_llm_offline(warnings, str(exc))
            urls = []
            for candidate in candidates or []:
                item = _cand_dict(candidate)
                url = item.get("url") if isinstance(item, dict) else None
                urls.append(url or "<unknown url>")
            return matcher.MatchBatchResult(all_decisions=[], confirmed_matches=[], skipped=urls)

    return guarded


def _save_run_record(gem_url: str, result: dict, trace_id: str, state: str = "done") -> None:
    """One small record per run, so the app can list recent runs after a
    restart without scanning the trace folder."""
    comparison = result.get("comparison") or {}
    product = result.get("gem_product") or {}
    record = {
        "run_id": trace_id,
        "gem_url": gem_url,
        "title": product.get("title"),
        "state": state,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "cheapest_price": comparison.get("cheapest_match_price"),
        "match_count": len(result.get("matches") or []),
        "from_cache": bool(result.get("from_cache")),
        # The full v2 result, so "recent runs" can reopen it after a restart
        # without keeping every trace in memory. summarize() hides it from
        # the list itself.
        "result": result,
    }
    try:
        runs_store.save_run(record)
    except OSError as e:
        logger.warning("Could not save the run list entry for %s: %s", trace_id, e)


# --- cache ------------------------------------------------------------------------

def _model_names() -> list[str]:
    """Which models can answer, without touching any API key value."""
    try:
        endpoints, _ = llm_router.load_endpoints()
    except Exception as e:  # noqa: BLE001 - a broken provider file must not stop a run
        logger.warning("Could not read the LLM provider list for the cache key: %s", e)
        return []
    return sorted(endpoint.name for endpoint in endpoints if endpoint.enabled)


def _answer_hash() -> str:
    """A short fingerprint of the settings that change what a run answers.

    Two runs with different confidence thresholds, price bands, prefilter
    size or model list must not replay each other's cached result.
    """
    settings = {
        "confidence_threshold": MATCH_CONFIDENCE_THRESHOLD,
        "price_low_ratio": PRICE_SANITY_LOW_RATIO,
        "price_high_ratio": PRICE_SANITY_HIGH_RATIO,
        "prefilter_top": PREFILTER_TOP,
        "retry": RETRY_ENABLED,
        "discovery": DISCOVERY_ENABLED,
        "models": _model_names(),
    }
    blob = json.dumps(settings, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _cache_path(gem_url: str) -> Path:
    material = f"{gem_url.strip()}|{_answer_hash()}|{RESULT_VERSION}"
    key = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
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

_trace_writes_since_rotation = 0


def _rotate_traces() -> None:
    """Keep the trace folder small: delete files over the count cap and files
    older than TRACE_MAX_AGE_DAYS. Unbounded growth was ~100-200KB per live
    run forever; the scan runs every TRACE_ROTATE_EVERY-th write instead of
    on every single run so the stat() calls are not paid per run."""
    try:
        traces = sorted(TRACES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        logger.debug("Trace rotation skipped: %s", e)
        return
    cutoff = time.time() - TRACE_MAX_AGE_DAYS * 86400
    for index, path in enumerate(traces):
        try:
            too_many = index >= TRACE_MAX_FILES
            too_old = TRACE_MAX_AGE_DAYS > 0 and path.stat().st_mtime < cutoff
            if too_many or too_old:
                path.unlink(missing_ok=True)
        except OSError as e:
            logger.debug("Could not remove trace %s: %s", path, e)


def _write_trace(trace: dict) -> str:
    global _trace_writes_since_rotation
    trace_id = trace["trace_id"]
    path = TRACES_DIR / f"{trace_id}.json"
    try:
        # E7: compact JSON — nothing reads traces by eye (the API serves them
        # parsed); indent=2 only added air to 100-200KB files. Keys are
        # scrubbed here because the trace is also served by the API.
        path.write_text(json.dumps(redact_obj(trace), ensure_ascii=False, default=str), encoding="utf-8")
    except OSError as e:
        logger.warning("Trace write failed for %s: %s — pipeline result is still valid, just unlogged.", trace_id, e)
        return trace_id
    _trace_writes_since_rotation += 1
    if _trace_writes_since_rotation < TRACE_ROTATE_EVERY:
        return trace_id
    _trace_writes_since_rotation = 0
    _rotate_traces()
    return trace_id


# --- helpers ------------------------------------------------------------------------

def _search_marketplaces(
    flipkart_search_fn,
    amazon_search_fn,
    queries: list[str],
    *,
    again: bool = False,
    normalized=None,
    skip_flipkart: bool = False,
    skip_amazon: bool = False,
):
    """Search Flipkart and Amazon; the default parallelism follows the
    GEM_PARALLEL setting, so tests patching pipeline.PARALLEL_SEARCH keep
    working. The work itself is core/searches.py."""
    return _search_marketplaces_impl(
        flipkart_search_fn,
        amazon_search_fn,
        queries,
        again=again,
        normalized=normalized,
        skip_flipkart=skip_flipkart,
        skip_amazon=skip_amazon,
        parallel=PARALLEL_SEARCH,
    )


def _prefilter_top() -> int:
    """GEM_PREFILTER_TOP, except that GEM_STRICT=1 means no filter at all."""
    return 0 if STRICT_MODE else PREFILTER_TOP


def _prefilter_candidates(candidates: list, normalized) -> tuple[list, list]:
    """Keep only the best N titled listings per shop (default 10) before
    spending LLM calls. GEM_PREFILTER_TOP=0 or GEM_STRICT=1 returns the
    input untouched."""
    top = _prefilter_top()
    if top <= 0:
        return candidates, []
    return prefilter.filter_candidates(candidates, normalized, top)


def _similar_products(normalized, candidates: list, warnings: list) -> list:
    """Stage 6.5: one LLM call, only when the run found nothing at all. The
    listings are 'same kind of product, not the same product', so they are
    never used for the cheapest price. The work itself lives in core/similar.py."""
    return similar_module.find_similar(normalized, candidates, warnings)


LANE_FOR_DOMAIN = {"flipkart.com": "flipkart", "amazon.in": "amazon"}


def _norm_domain(domain) -> str:
    return str(domain or "").lower().removeprefix("www.")


def _shop_entries(
    *,
    flipkart_results,
    amazon_results,
    discovery_results,
    flipkart_candidates,
    amazon_candidates,
    generic_candidates,
    match_result,
    prefilter_dropped,
    lane_errors,
    lane_seconds,
    nothing_found: bool,
    retried: set | None = None,
    discovered: bool = True,
) -> dict:
    """One card per shop (Stage 7.2). Pure data in, pure data out.

    Rules: blocked/errored search -> not_searched; a listing that could not be
    checked -> incomplete (never not_found); a lane over its time limit ->
    timed_out; at least one possible but no match -> possible; searched
    cleanly and nothing -> not_found."""
    retried = set(retried or ())
    decisions = list(getattr(match_result, "all_decisions", None) or [])
    confirmed = list(getattr(match_result, "confirmed_matches", None) or [])
    possible = list(getattr(match_result, "possible_matches", None) or [])
    skipped = list(getattr(match_result, "skipped", None) or [])

    domains = {"flipkart.com", "amazon.in"}
    for candidate in generic_candidates or []:
        domain = _norm_domain(_cand_dict(candidate).get("source_domain"))
        if domain:
            domains.add(domain)

    entries: dict = {}
    for domain in sorted(domains):
        if domain == "flipkart.com":
            results, candidates = list(flipkart_results or ()), list(flipkart_candidates or ())
        elif domain == "amazon.in":
            results, candidates = list(amazon_results or ()), list(amazon_candidates or ())
        else:
            results = []
            candidates = [
                c for c in (generic_candidates or []) if _norm_domain(_cand_dict(c).get("source_domain")) == domain
            ]

        seen_keys = {_listing_key(_cand_dict(c).get("url")) for c in candidates}
        seen = len([key for key in seen_keys if key])
        dropped = sum(1 for d in (prefilter_dropped or ()) if _norm_domain(d.get("source_domain")) == domain)
        checked = sum(1 for d in decisions if _norm_domain(d.get("source_domain")) == domain)
        matches = sum(1 for d in confirmed if _norm_domain(d.get("source_domain")) == domain)
        possibles = sum(1 for d in possible if _norm_domain(d.get("source_domain")) == domain)
        unchecked = sum(
            1 for url in skipped if isinstance(url, str) and _norm_domain(urlparse(url).netloc) == domain
        )
        lane = LANE_FOR_DOMAIN.get(domain, "other_shops")
        seconds = (lane_seconds or {}).get(lane)
        blocked = [r for r in results if _cand_dict(r).get("blocked")]
        errored = [r for r in results if _cand_dict(r).get("error")]
        lane_error = next((e for e in (lane_errors or []) if str(e).startswith(lane)), "")
        # A lane that CRASHED is not retried: the same call would crash again.
        # A blocked/errored search IS retryable (spec 7.2) - new words may
        # reach a shop the first query could not.
        hard_failure = bool(lane_error)

        if matches:
            status, reason = result_module.SHOP_FOUND, ""
        elif blocked:
            status, reason = result_module.SHOP_NOT_SEARCHED, "blocked/CAPTCHA"
        elif errored:
            status, reason = result_module.SHOP_NOT_SEARCHED, str(_cand_dict(errored[0]).get("error"))
        elif lane_error:
            status, reason = result_module.SHOP_INCOMPLETE, lane_error
        elif seconds is not None and seconds > LANE_TIME_LIMIT_SECONDS:
            status, reason = result_module.SHOP_TIMED_OUT, f"lane took {seconds:.0f} s (limit {LANE_TIME_LIMIT_SECONDS:.0f} s)"
        elif unchecked:
            status, reason = result_module.SHOP_INCOMPLETE, f"{unchecked} listing(s) could not be checked"
        elif possibles:
            status, reason = result_module.SHOP_POSSIBLE, f"{possibles} possible listing(s), none confirmed"
        elif seen == 0 and not results:
            status, reason = result_module.SHOP_NOT_SEARCHED, "the search returned no listings"
        else:
            status, reason = result_module.SHOP_NOT_FOUND, "searched, nothing confirmed"

        retryable = (
            status in result_module.UNFINISHED_STATUSES
            or (status == result_module.SHOP_NOT_FOUND and nothing_found)
        ) and not hard_failure and not (status == result_module.SHOP_FOUND)
        # The discovery lane's retry covers every other shop at once.
        retried_here = domain in retried or ("other" in retried and domain not in LANE_FOR_DOMAIN)
        if retried_here:
            # One retry per shop per run: the flag tells the caller there is
            # nothing left to spend on this shop.
            retryable = False

        entries[domain] = result_module.shop_entry(
            status=status,
            reason=reason,
            seen=seen,
            dropped=dropped,
            checked=checked,
            matches=matches,
            retried=retried_here,
            retryable=retryable,
            seconds=seconds,
        )

    # The discovery lane itself, summarised: a crash there is not any one
    # shop's fault, and the page needs one card that says so.
    generic_entries = [e for name, e in entries.items() if name not in LANE_FOR_DOMAIN.values() and name not in ("flipkart.com", "amazon.in")]
    other_lane_error = next((e for e in (lane_errors or []) if str(e).startswith("other_shops")), "")
    discovery_rows = [
        r.to_dict() if hasattr(r, "to_dict") else r for r in (discovery_results if isinstance(discovery_results, (list, tuple)) else [])
    ]
    discovery_blocked = any(r.get("blocked") for r in discovery_rows if isinstance(r, dict))
    if not discovered:
        other_status, other_reason = result_module.SHOP_NOT_SEARCHED, "discovery is switched off"
    elif other_lane_error:
        other_status, other_reason = result_module.SHOP_INCOMPLETE, other_lane_error
    elif discovery_blocked:
        other_status, other_reason = result_module.SHOP_NOT_SEARCHED, "discovery blocked/CAPTCHA"
    elif any(e["status"] == result_module.SHOP_FOUND for e in generic_entries):
        other_status, other_reason = result_module.SHOP_FOUND, ""
    elif any(e["status"] in result_module.UNFINISHED_STATUSES for e in generic_entries):
        other_status, other_reason = result_module.SHOP_INCOMPLETE, "some other shops could not be searched"
    elif any(e["status"] == result_module.SHOP_POSSIBLE for e in generic_entries):
        other_status, other_reason = result_module.SHOP_POSSIBLE, "possible listings only"
    else:
        other_status, other_reason = result_module.SHOP_NOT_FOUND, "searched, nothing confirmed"
    other_retryable = (
        other_status in result_module.UNFINISHED_STATUSES and discovered and not other_lane_error
    ) or (other_status == result_module.SHOP_NOT_FOUND and nothing_found)
    if "other" in retried:
        other_retryable = False
    entries["other"] = result_module.shop_entry(
        status=other_status,
        reason=other_reason,
        seen=sum(e["seen"] for e in generic_entries),
        dropped=sum(e["dropped"] for e in generic_entries),
        checked=sum(e["checked"] for e in generic_entries),
        matches=sum(e["matches"] for e in generic_entries),
        retried="other" in retried,
        retryable=other_retryable,
        seconds=(lane_seconds or {}).get("other_shops"),
    )
    return entries


def _aggregate(
    *,
    gem_url: str,
    gem_product_dict: dict,
    normalized: dict,
    match_result,
    shops: dict,
    similar: list,
    warnings: list,
    search_issues: list,
    tried_domains: set,
    trace_id: str,
    state: str,
    from_cache: bool = False,
) -> tuple[dict, list, list]:
    """Turn the pass's pieces into a v2 result (one place, called for the
    first result and again after the retry). Returns (result, matches, possible)."""
    confirmed = list(getattr(match_result, "confirmed_matches", None) or [])
    possible = list(getattr(match_result, "possible_matches", None) or [])
    skipped = list(getattr(match_result, "skipped", None) or [])

    matched_domains = {_norm_domain(m.get("source_domain")) for m in confirmed}
    possible_domains = {_norm_domain(m.get("source_domain")) for m in possible if m.get("source_domain")}
    skipped_domains: set = set()
    for url in skipped:
        if isinstance(url, str) and url.startswith("http"):
            domain = _norm_domain(urlparse(url).netloc)
            if domain:
                skipped_domains.add(domain)

    verify_incomplete = sorted((tried_domains - matched_domains) & skipped_domains)
    not_found_on = sorted(tried_domains - matched_domains - skipped_domains - possible_domains)
    for domain in verify_incomplete:
        search_issues.append(f"{domain}: candidates skipped (verify incomplete) — not counted as 'not found'")
    for domain in sorted((tried_domains - matched_domains) & possible_domains):
        search_issues.append(f"{domain}: possibly the same product (not confirmed) — not counted as 'not found'")

    seen_issues: set = set()
    deduped = [i for i in search_issues if not (i in seen_issues or seen_issues.add(i))]
    search_issues[:] = deduped

    gem_price = gem_product_dict.get("gem_price")
    if gem_price is None:
        message = "GeM price not extracted — price-sanity gate is OFF and savings comparison unavailable."
        if message not in warnings:
            warnings.append(message)
    for match in confirmed:
        price = match.get("price_used")
        if gem_price is not None and price is not None:
            match["delta_vs_gem"] = price - gem_price
            match["cheaper_than_gem"] = price < gem_price
    priced = [m.get("price_used") for m in confirmed if m.get("price_used") is not None]
    comparison = {
        "gem_price": gem_price,
        "gem_price_type": gem_product_dict.get("gem_price_type", "unknown"),
        "cheapest_match_price": min(priced) if priced else None,
        "savings_vs_gem": (gem_price - min(priced)) if (gem_price is not None and priced) else None,
    }

    result = result_module.build_result(
        gem_url=gem_url,
        gem_product=gem_product_dict,
        normalized=normalized,
        shops=shops,
        matches=confirmed,
        possible_matches=possible,
        similar=similar,
        warnings=warnings,
        search_issues=search_issues,
        not_found_on=not_found_on,
        skipped=skipped,
        comparison=comparison,
        state=state,
        from_cache=from_cache,
        trace_id=trace_id,
    )
    return result, confirmed, possible


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
    lane_errors: Optional[list] = None,
    lane_seconds: Optional[dict] = None,
) -> list:
    """GEM_PARALLEL=1 first pass: three lanes — Flipkart, Amazon and
    discovery+generic — run at the same time, and each lane verifies its own
    candidates as soon as its search returns, so the LLM work of a fast lane
    overlaps the slow Flipkart lane instead of waiting for every search to
    finish. Returns one (results, candidates, match_result) tuple per lane,
    in that order."""
    lane_seconds = lane_seconds if lane_seconds is not None else {}
    events.emit("stage", name="search_both", label="Searching Flipkart, Amazon and other shops at the same time")
    if DISCOVERY_ENABLED:
        events.emit("stage", name="discovery", label="Looking for other shops")

    def flipkart_lane():
        started = time.monotonic()
        events.emit("shop_started", shop="flipkart.com")
        results, candidates = _search_direct(flipkart_search_fn, queries, label="flipkart", normalized=normalized)
        lane_match, dropped = _verify_lane(normalized, candidates, match_fn, name="flipkart", label="the Flipkart listings")
        lane_seconds["flipkart"] = time.monotonic() - started
        return results, candidates, lane_match, dropped

    def amazon_lane():
        started = time.monotonic()
        events.emit("shop_started", shop="amazon.in")
        results, candidates = _search_direct(amazon_search_fn, queries, label="amazon", normalized=normalized)
        lane_match, dropped = _verify_lane(normalized, candidates, match_fn, name="amazon", label="the Amazon listings")
        lane_seconds["amazon"] = time.monotonic() - started
        return results, candidates, lane_match, dropped

    def other_shops_lane():
        started = time.monotonic()
        events.emit("shop_started", shop="other")
        if not DISCOVERY_ENABLED:
            lane_seconds["other_shops"] = time.monotonic() - started
            return [], [], matcher.MatchBatchResult(), []
        results, candidates = _run_discovery_and_generic_scrape(
            queries, None, discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn
        )
        seen_domains: set = set()
        for candidate in candidates:
            shop = _norm_domain(_cand_dict(candidate).get("source_domain"))
            if shop and shop not in seen_domains:
                seen_domains.add(shop)
                events.emit("shop_started", shop=shop)
        lane_match, dropped = _verify_lane(normalized, candidates, match_fn, name="other_shops", label="other shops' listings")
        lane_seconds["other_shops"] = time.monotonic() - started
        return results, candidates, lane_match, dropped

    # One lane crashing (for example Chrome dying) must not throw away the
    # other lanes' good results. Each lane is wrapped: a crash becomes an
    # event plus a line in lane_errors, and the lane counts as empty. Only
    # when ALL lanes crashed is there nothing to continue with, and the first
    # error is raised.
    if lane_errors is None:
        lane_errors = []
    crashes: list = []

    def guarded(lane_name, lane_function):
        def run_lane():
            try:
                return lane_function()
            except Exception as exc:  # noqa: BLE001 - any lane failure is isolated here
                logger.warning("Lane %s failed: %s: %s", lane_name, type(exc).__name__, exc)
                events.emit("lane_failed", lane=lane_name, error=f"{type(exc).__name__}: {exc}")
                lane_errors.append(f"{lane_name}: {type(exc).__name__}: {exc}")
                crashes.append(exc)
                return [], [], matcher.MatchBatchResult(), []
        return run_lane

    lane_results = run_many(
        [
            guarded("flipkart", flipkart_lane),
            guarded("amazon", amazon_lane),
            guarded("other_shops", other_shops_lane),
        ],
        workers=3,
    )
    if len(crashes) >= 3:
        raise crashes[0]
    return lane_results


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
    possible: list = []
    for lane in lane_results:
        entries = lane.get("possible_matches") if isinstance(lane, dict) else getattr(lane, "possible_matches", None)
        for d in entries or []:
            if id(d) in kept_ids:
                possible.append(d)
    possible.sort(
        key=lambda d: (
            d.get("price_used") is None,
            d.get("price_used") if d.get("price_used") is not None else 0.0,
        )
    )
    not_checked: list[str] = []
    seen_not_checked: set = set()
    for lane in lane_results:
        entries = lane.get("not_checked") if isinstance(lane, dict) else getattr(lane, "not_checked", None)
        for u in entries or []:
            key = _listing_key(u) or f"__noid_nc_{len(not_checked)}"
            if key in seen_not_checked:
                continue
            seen_not_checked.add(key)
            not_checked.append(u)
    return matcher.MatchBatchResult(
        all_decisions=kept,
        confirmed_matches=confirmed,
        skipped=skipped,
        possible_matches=possible,
        not_checked=not_checked,
    )


def _merge_rows(first: list, second: list) -> list:
    """Rows from two passes, first occurrence wins (by URL, else identity)."""
    merged: list = []
    seen: set = set()
    for row in list(first or []) + list(second or []):
        if not isinstance(row, dict):
            continue
        key = _listing_key(row.get("candidate_url") or row.get("url")) or f"__row_{id(row)}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def _merge_decisions(first: list, second: list) -> list:
    """Same for decision rows — a retry must not duplicate a first-pass verdict."""
    return _merge_rows(first, second)


# --- main entry point --------------------------------------------------------------

def _run_pipeline(
    gem_url: str,
    refresh: bool = False,
    *,
    extract_fn=gem_extractor.extract_gem_product,
    normalize_fn=llm_client.normalize_product,
    flipkart_search_fn=None,  # None -> the run's own one-Chrome browser (see below)
    amazon_search_fn=amazon_scraper.search,
    discover_fn=discovery.discover_for_queries,
    generic_scrape_fn=generic_scraper.scrape,
    match_fn=matcher.match_candidates,
    expand_search_fn=llm_client.expand_search,
    use_cache: bool = True,
) -> dict:
    events.emit("run_started", gem_url=gem_url, refresh=refresh)
    if not refresh and use_cache:
        cached = _cache_get(gem_url)
        if cached is not None:
            result = dict(cached)
            result["from_cache"] = True
            events.emit("cache_hit", gem_url=gem_url)
            _emit_finished(result)
            events.emit("done", state=result.get("state"), from_cache=True, trace_id=result.get("trace_id"))
            return result

    trace_id = uuid.uuid4().hex[:16]
    trace: dict = {
        "trace_id": trace_id,
        "result_version": RESULT_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gem_url": gem_url,
        "stages": {},
    }

    warnings: list[str] = []
    lane_seconds: dict = {}
    flipkart_browser = None
    try:
        if flipkart_search_fn is None:
            # One Chrome for the whole run — every query and the retry — and it
            # starts building NOW, so the 3-8 s startup hides behind the GeM
            # page fetch and the LLM normalize instead of delaying the first
            # search. Tests and the replay tool pass their own search function,
            # so no browser is ever started for them.
            flipkart_browser = flipkart_scraper.FlipkartBrowser()
            flipkart_browser.start()
            flipkart_search_fn = flipkart_browser.search

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
        events.emit("product_ready", title=gem_product_dict.get("title"), price=gem_product_dict.get("gem_price"))

        # --- stage 2: normalize ----------------------------------------------------------
        events.emit("stage", name="normalize", label="Working out what the product really is")
        try:
            normalized = normalize_fn(gem_product_dict)
        except GatewayExhausted as exc:
            # Every LLM allowance is gone for the day. The comparison is
            # still worth running with the GeM title as the identity; the
            # listings simply cannot be verified (reported per lane below).
            _note_llm_offline(warnings, str(exc))
            normalized = _identity_without_llm(gem_product_dict)
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

        # Stage 6.3: verdicts are shared across the lanes of one run — the
        # same listing title + price for the same product is one question.
        if match_fn is matcher.match_candidates and REUSE_VERDICTS:
            match_fn = partial(match_fn, verdict_cache={})

        # Same deal at the matching step: a lane whose LLM call finds every
        # endpoint busy or used up reports its listings as skipped.
        match_fn = _guarded_match(match_fn, warnings)

        if flipkart_browser is not None:
            # C6: the Flipkart page loop stops early when page 1 already holds
            # enough relevant cards — give it the product's words to judge by.
            flipkart_search_fn = partial(flipkart_browser.search, product=prefilter.product_tokens(normalized))

        # --- stage 3/4 first pass: Flipkart + Amazon direct search -----------------------
        # Every normalized query is eligible (early-exit once one query has
        # enough candidates), not just queries[0].
        prefilter_dropped: list = []
        lane_errors: list = []  # filled only by the parallel lanes (see _run_first_pass_lanes)
        # GEM_STRICT=1 keeps the lanes (searches still run together) but turns
        # the pre-filter off and the verifier back to one listing at a time.
        lane_mode = PARALLEL_SEARCH or STRICT_MODE
        if lane_mode:
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
                lane_errors=lane_errors,
                lane_seconds=lane_seconds,
            )
            prefilter_dropped = list(flipkart_dropped) + list(amazon_dropped) + list(other_shops_dropped)
            if _prefilter_top() > 0:
                trace["stages"]["prefilter"] = {"top": _prefilter_top(), "dropped": prefilter_dropped}
        else:
            events.emit("shop_started", shop="flipkart.com")
            events.emit("shop_started", shop="amazon.in")
            started = time.monotonic()
            flipkart_results, flipkart_candidates, amazon_results, amazon_candidates = _search_marketplaces(
                flipkart_search_fn, amazon_search_fn, queries, normalized=normalized
            )
            lane_seconds["flipkart"] = time.monotonic() - started
            lane_seconds["amazon"] = lane_seconds["flipkart"]
            discovery_results = generic_candidates = None
        trace["stages"]["flipkart_search"] = [r.to_dict() if hasattr(r, "to_dict") else r for r in flipkart_results]
        trace["stages"]["amazon_search"] = [r.to_dict() if hasattr(r, "to_dict") else r for r in amazon_results]

        # Search health decides who's honestly "searched" vs who failed to scrape.
        search_issues: list[str] = list(lane_errors)
        tried_domains: set = set()
        for r in flipkart_results + amazon_results:
            _record_search_health(r, tried_domains, search_issues)

        # --- stage 3/4 first pass: open discovery + generic scrape for everything else ---
        if not lane_mode:
            events.emit("shop_started", shop="other")
            started = time.monotonic()
            if DISCOVERY_ENABLED:
                events.emit("stage", name="discovery", label="Looking for other shops")
                discovery_results, generic_candidates = _run_discovery_and_generic_scrape(
                    queries, None, discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn
                )
            else:
                discovery_results, generic_candidates = [], []
            lane_seconds["other_shops"] = time.monotonic() - started
            seen_domains: set = set()
            for candidate in generic_candidates:
                shop = _norm_domain(_cand_dict(candidate).get("source_domain"))
                if shop and shop not in seen_domains:
                    seen_domains.add(shop)
                    events.emit("shop_started", shop=shop)
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
        # Everything the run ever saw as a usable listing — the similar-products
        # call (Stage 6.5) draws from this, retry results included.
        similar_pool = list(all_candidates)

        # --- stage 5: match ---------------------------------------------------------------
        if lane_mode:
            # Each lane already checked its own listings (and emitted its own
            # match stage); merging keeps one row per listing, first lane wins.
            match_result = _merge_match_results([flipkart_match, amazon_match, other_shops_match])
        else:
            events.emit("stage", name="match", label="Checking which listings are the same product")
            match_input, dropped_now = _prefilter_candidates(all_candidates, normalized)
            prefilter_dropped.extend(dropped_now)
            events.emit("candidates_total", count=len(match_input))
            match_result = match_fn(normalized, match_input)
            if _prefilter_top() > 0:
                trace["stages"]["prefilter"] = {"top": _prefilter_top(), "dropped": prefilter_dropped}
        final_match_result = match_result
        trace["stages"]["matching"] = match_result.to_dict() if hasattr(match_result, "to_dict") else match_result

        confirmed = list(match_result.confirmed_matches)
        first_possible = list(getattr(match_result, "possible_matches", []) or [])

        # --- Stage 7: first honest result, before any retry -------------------------------
        # A22/Stage 7.3: the page gets a full answer now (state "shown") and the
        # retry, if it happens, updates it. Shops that are still unfinished say
        # so instead of pretending to be "not found".
        first_shops = _shop_entries(
            flipkart_results=flipkart_results,
            amazon_results=amazon_results,
            discovery_results=discovery_results,
            flipkart_candidates=flipkart_candidates,
            amazon_candidates=amazon_candidates,
            generic_candidates=generic_candidates,
            match_result=match_result,
            prefilter_dropped=prefilter_dropped,
            lane_errors=lane_errors,
            lane_seconds=lane_seconds,
            nothing_found=not confirmed and not first_possible,
            discovered=bool(DISCOVERY_ENABLED),
        )
        trace["stages"]["shops"] = first_shops
        first_result, _, _ = _aggregate(
            gem_url=gem_url,
            gem_product_dict=gem_product_dict,
            normalized=normalized,
            match_result=match_result,
            shops=first_shops,
            similar=[],
            warnings=warnings,
            search_issues=search_issues,
            tried_domains=tried_domains,
            trace_id=trace_id,
            state="shown",
        )
        for name, entry in first_shops.items():
            events.emit("shop_finished", shop=name, **entry)
        # Saved before the event so a listener that reads the record (the CLI)
        # never races the write.
        _save_run_record(gem_url, first_result, trace_id, state="shown")
        _emit_finished(first_result)

        # --- retry: only the shops that are honestly retryable ---------------------------
        retry_trace = {"triggered": False}
        retried_shops: set = set()
        retry_shops = retry_module.retryable_shops(
            first_shops, nothing_found=not confirmed and not first_possible
        )
        if first_shops and RETRY_ENABLED and retry_shops:
            retry_trace["state"] = "updating"
            retry_trace["shops"] = sorted(retry_shops)
            events.emit("retry_started", shops=sorted(retry_shops))
            events.emit("stage", name="retry", label="Nothing matched yet, asking the LLM for better search words")
            # expand_search is OPTIONAL logic — if it throws (bad JSON, 429,
            # timeout) the run must survive on its first-pass results. It
            # previously propagated and destroyed a fully successful run.
            try:
                expansion = expand_search_fn(normalized, list(queries), sorted(tried_domains | set(KNOWN_MARKETPLACE_DOMAINS)))
            except GatewayExhausted as e:
                # Spec 7.3/7.4: a used-up LLM cannot suggest new words, so the
                # retry is skipped and the fact is a warning, not an error.
                retry_trace["expansion_error"] = f"{type(e).__name__}: {e}"
                retry_trace["skipped_reason"] = "LLM limits used up"
                _note_llm_offline(warnings, f"Retry skipped: {e}")
                logger.warning("expand_search found the LLM used up (%s) - skipping retry.", e)
                expansion = {"new_queries": [], "suggested_domains": [], "notes": ""}
            except Exception as e:
                retry_trace["expansion_error"] = f"{type(e).__name__}: {e}"
                logger.warning("expand_search failed (%s) - skipping retry, keeping first-pass results.", e)
                expansion = {"new_queries": [], "suggested_domains": [], "notes": ""}
            retry_trace["expansion"] = expansion
            events.emit("retry_plan", new_queries=_str_list(expansion.get("new_queries")), notes=expansion.get("notes", ""))

            retry_queries = retry_module.fresh_queries(expansion, queries)
            # Strict-mode Indian filter: the prompt asks for Indian domains but
            # doesn't enforce it — a foreign suggestion (bestbuy.com) must not
            # reopen open-mode .com discovery.
            retry_domains = [d for d in _str_list(expansion.get("suggested_domains")) if discovery._is_indian_retail(d)] or None  # None -> open discovery

            if retry_queries:
                # Only now is a retry really happening: new words exist, so the
                # cards can honestly say `retried`.
                retried_shops = set(retry_shops)
                retry_trace["triggered"] = True
                # Re-search the two DIRECT scrapers with the expanded queries —
                # expansion used to feed only the (dead) Google discovery path,
                # so a bad primary query on the working marketplaces was never
                # retried with the LLM's better phrasing. All retry_queries are
                # eligible, not just [0].
                # Stage 7.4: shops that are not retryable (found/possible, or a
                # lane that crashed) are skipped - they already answered. A
                # blocked shop IS retried with the new words (spec 7.2); the
                # block is recorded so a repeat is visible in the trace.
                skip_flipkart = "flipkart.com" not in retried_shops
                skip_amazon = "amazon.in" not in retried_shops
                retry_other = "other" in retried_shops or bool(
                    retried_shops - {"flipkart.com", "amazon.in", "other"}
                )
                try:
                    (
                        retry_flipkart_results,
                        retry_flipkart_candidates,
                        retry_amazon_results,
                        retry_amazon_candidates,
                    ) = _search_marketplaces(
                        flipkart_search_fn,
                        amazon_search_fn,
                        retry_queries,
                        again=True,
                        normalized=normalized,
                        skip_flipkart=skip_flipkart,
                        skip_amazon=skip_amazon,
                    )
                    retry_search_failed = False
                except Exception as e:  # noqa: BLE001 - the retry must not kill the run
                    retry_trace["search_error"] = f"{type(e).__name__}: {e}"
                    logger.warning("Retry search failed (%s) - keeping the first-pass result.", e)
                    retry_flipkart_results = retry_flipkart_candidates = []
                    retry_amazon_results = retry_amazon_candidates = []
                    retry_search_failed = True
                retry_trace["blocked_first_pass"] = [
                    name for name, skipped in (("flipkart", skip_flipkart), ("amazon", skip_amazon)) if skipped
                ]
                trace["stages"]["flipkart_search_retry"] = [
                    r.to_dict() if hasattr(r, "to_dict") else r for r in retry_flipkart_results
                ]
                trace["stages"]["amazon_search_retry"] = [
                    r.to_dict() if hasattr(r, "to_dict") else r for r in retry_amazon_results
                ]
                for r in retry_flipkart_results + retry_amazon_results:
                    _record_search_health(r, tried_domains, search_issues)

                if DISCOVERY_ENABLED and retry_other and not retry_search_failed:
                    try:
                        retry_discovery_results, retry_generic_candidates = _run_discovery_and_generic_scrape(
                            retry_queries, retry_domains, discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn
                        )
                    except Exception as e:  # noqa: BLE001 - the retry must not kill the run
                        retry_trace["discovery_error"] = f"{type(e).__name__}: {e}"
                        logger.warning("Retry discovery failed (%s) - keeping the first-pass result.", e)
                        retry_discovery_results, retry_generic_candidates = [], []
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
                similar_pool.extend(
                    list(retry_flipkart_candidates) + list(retry_amazon_candidates) + list(retry_generic_candidates)
                )
                retry_pool, dropped_now = _prefilter_candidates(retry_pool, normalized)
                prefilter_dropped.extend(dropped_now)
                if _prefilter_top() > 0:
                    trace["stages"]["prefilter"] = {"top": _prefilter_top(), "dropped": prefilter_dropped}

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
                if retry_pool:
                    retry_match_result = match_fn(normalized, retry_pool)
                else:
                    retry_match_result = matcher.MatchBatchResult()
                if reused_decisions:
                    # final all_decisions must still show what happened with the
                    # reused listings — only the confirmed/skipped come from the
                    # retry itself. Possible verdicts from the first pass are
                    # still possible; do not lose them.
                    retry_match_result = matcher.MatchBatchResult(
                        all_decisions=reused_decisions + list(retry_match_result.all_decisions),
                        confirmed_matches=list(retry_match_result.confirmed_matches),
                        skipped=list(retry_match_result.skipped),
                        possible_matches=list(retry_match_result.possible_matches)
                        + [d for d in reused_decisions if d.get("outcome") == "possible"],
                        not_checked=list(retry_match_result.not_checked),
                    )
                retry_trace["matching"] = retry_match_result.to_dict() if hasattr(retry_match_result, "to_dict") else retry_match_result

                # Stage 7.4: keep what the first pass already confirmed — a
                # retry caused by one unfinished shop must not erase another
                # shop's match.
                final_match_result = matcher.MatchBatchResult(
                    all_decisions=_merge_decisions(
                        list(getattr(retry_match_result, "all_decisions", []) or []),
                        list(getattr(match_result, "all_decisions", []) or []),
                    ),
                    confirmed_matches=_merge_rows(
                        match_result.confirmed_matches, retry_match_result.confirmed_matches
                    ),
                    skipped=list(retry_match_result.skipped),
                    possible_matches=_merge_rows(
                        getattr(match_result, "possible_matches", []) or [],
                        getattr(retry_match_result, "possible_matches", []) or [],
                    ),
                    not_checked=list(getattr(retry_match_result, "not_checked", []) or []),
                )
                confirmed = list(final_match_result.confirmed_matches)
            else:
                retry_trace["skipped_reason"] = "expand_search returned no new queries to try"

        trace["stages"]["retry"] = retry_trace

        final_possible = list(getattr(final_match_result, "possible_matches", []) or [])

        # --- stage 6.5: similar products --------------------------------------------------
        # Only when the run found nothing at all — not one confirmed match and
        # not one possible one. A "similar" product is a different thing at a
        # different price, so it never feeds the cheapest/savings arithmetic.
        similar: list = []
        if not confirmed and not final_possible and similar_pool:
            similar = _similar_products(normalized, similar_pool, warnings)
        trace["stages"]["similar"] = similar

        final_shops = _shop_entries(
            flipkart_results=flipkart_results,
            amazon_results=amazon_results,
            discovery_results=discovery_results,
            flipkart_candidates=flipkart_candidates,
            amazon_candidates=amazon_candidates,
            generic_candidates=generic_candidates,
            match_result=final_match_result,
            prefilter_dropped=prefilter_dropped,
            lane_errors=lane_errors,
            lane_seconds=lane_seconds,
            nothing_found=not confirmed and not final_possible,
            retried=retried_shops if retry_trace["triggered"] else set(),
            discovered=bool(DISCOVERY_ENABLED),
        )
        trace["stages"]["shops"] = final_shops

        # --- comparison arithmetic — the actual point of the project ----------------------
        result, confirmed, final_possible = _aggregate(
            gem_url=gem_url,
            gem_product_dict=gem_product_dict,
            normalized=normalized,
            match_result=final_match_result,
            shops=final_shops,
            similar=similar,
            warnings=warnings,
            search_issues=search_issues,
            tried_domains=tried_domains,
            trace_id=trace_id,
            state="done",
        )
        trace["final_result"] = {
            "matches": confirmed,
            "possible_matches": final_possible,
            "similar": similar,
            "not_found_on": result.get("not_found_on"),
            "search_issues": search_issues,
        }

        if similar:
            events.emit("similar_ready", similar=similar)
        for name in sorted(retried_shops if retry_trace["triggered"] else ()):
            entry = final_shops.get(name)
            if entry:
                events.emit("shop_updated", shop=name, **entry)
        events.emit("run_updated", result=result)
        events.emit("done", state="done", trace_id=trace_id)
        trace["events"] = events.snapshot()
        _write_trace(trace)
        _save_run_record(gem_url, result, trace_id, state="done")
        if not use_cache:
            logger.info("Not caching this run: cache disabled (demo).")
        elif search_issues:
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
    finally:
        # The run owns its Chrome: every exit path (success, failure, early
        # return) goes through here, so it is quit exactly once.
        if flipkart_browser is not None:
            flipkart_browser.close()


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
