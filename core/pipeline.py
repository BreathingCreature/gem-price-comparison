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
That's not incidental - it's what makes this module's own orchestration
logic (sequencing, the retry trigger, cache, trace shape, not_found_on
computation) testable in isolation from whether the real scrapers/LLM
calls work, the same way the previous parts were tested independently.

Scope note on the retry step: expand_search's new_queries feed BOTH the
direct Flipkart/Amazon scrapers (not just discovery - a bad primary
phrasing never gets the LLM's own alternate tried on the working
marketplaces) and discovery+generic for sites those scrapers don't cover.

Scope note on the trace: it captures structured input/output at each
stage (queries used, candidates found, match decisions with their
reasons) via the to_dict() every input object already has. It does NOT
capture the raw LLM prompt/response text - doing that would mean
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
from dataclasses import dataclass, field
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

# A direct search is "enough" once it has this many RELEVANT candidates -
# stop trying further normalized queries so a 3-query normalize doesn't cost
# 3 Selenium runs when the first query already returned a full page of
# results, while junk from query 1 does not end the loop.
DIRECT_SEARCH_MIN_CANDIDATES = 5
# the rotation scan stats every trace file; running it every 25th write
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
    record_search_health as _record_search_health,
    run_discovery_and_generic_scrape as _run_discovery_and_generic_scrape,
    search_direct as _search_direct,
    search_marketplaces as _search_marketplaces_impl,
)


def _str_list(value) -> list[str]:
    """Coerce to a list of non-empty strings. LLM fields sometimes arrive as
    a bare string - queries[0] on a string is the first CHARACTER, and
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
    'not today' - both are the same to the current report."""

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
        logger.warning("Cache read failed for %s: %s - treating as a miss.", gem_url, e)
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
        logger.warning("Cache write failed for %s: %s - continuing without caching this result.", gem_url, e)


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
        # compact JSON - nothing reads traces by eye (the API serves them
        # parsed); indent=2 only added air to 100-200KB files. Keys are
        # scrubbed here because the trace is also served by the API.
        path.write_text(json.dumps(redact_obj(trace), ensure_ascii=False, default=str), encoding="utf-8")
    except OSError as e:
        logger.warning("Trace write failed for %s: %s - pipeline result is still valid, just unlogged.", trace_id, e)
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
    """One LLM call, only when the run found nothing at all. The
    listings are 'same kind of product, not the same product', so they are
    never used for the cheapest price. The work itself lives in core/similar.py."""
    return similar_module.find_similar(normalized, candidates, warnings)


LANE_FOR_DOMAIN = {"flipkart.com": "flipkart", "amazon.in": "amazon"}

# B2B / bulk marketplaces are allowed, but their price can be wholesale, so a
# match there is labelled "B2B" instead of being read as a retail price.
BULK_MARKETPLACES = {"indiamart.com"}


def _norm_domain(domain) -> str:
    return str(domain or "").lower().removeprefix("www.")


@dataclass
class _ShopFacts:
    domain: str
    results: list
    candidates: list
    seen: int
    dropped: int
    checked: int
    matches: int
    possibles: int
    unchecked: int
    lane_error: str
    seconds: float | None


def _shop_domains(generic_candidates) -> set:
    domains = {"flipkart.com", "amazon.in"}
    for candidate in generic_candidates or []:
        domain = _norm_domain(_cand_dict(candidate).get("source_domain"))
        if domain:
            domains.add(domain)
    return domains


def _shop_sources(domain, flipkart_results, flipkart_candidates, amazon_results, amazon_candidates, generic_candidates):
    if domain == "flipkart.com":
        return list(flipkart_results or ()), list(flipkart_candidates or ())
    if domain == "amazon.in":
        return list(amazon_results or ()), list(amazon_candidates or ())
    results = []
    candidates = [
        c for c in (generic_candidates or ()) if _norm_domain(_cand_dict(c).get("source_domain")) == domain
    ]
    return results, candidates


def _shop_counts(domain, candidates, decisions, confirmed, possible, skipped, prefilter_dropped):
    seen_keys = {_listing_key(_cand_dict(c).get("url")) for c in candidates}
    seen = len([key for key in seen_keys if key])
    dropped = sum(1 for d in (prefilter_dropped or ()) if _norm_domain(d.get("source_domain")) == domain)
    checked = sum(1 for d in decisions if _norm_domain(d.get("source_domain")) == domain)
    matches = sum(1 for d in confirmed if _norm_domain(d.get("source_domain")) == domain)
    possibles = sum(1 for d in possible if _norm_domain(d.get("source_domain")) == domain)
    unchecked = sum(
        1 for url in skipped if isinstance(url, str) and _norm_domain(urlparse(url).netloc) == domain
    )
    return seen, dropped, checked, matches, possibles, unchecked


def _shop_facts(domain, results, candidates, counts, lane_error, lane_seconds) -> _ShopFacts:
    seen, dropped, checked, matches, possibles, unchecked = counts
    lane = LANE_FOR_DOMAIN.get(domain, "other_shops")
    return _ShopFacts(
        domain=domain,
        results=results,
        candidates=candidates,
        seen=seen,
        dropped=dropped,
        checked=checked,
        matches=matches,
        possibles=possibles,
        unchecked=unchecked,
        lane_error=lane_error,
        seconds=(lane_seconds or {}).get(lane),
    )


def _shop_status(facts: _ShopFacts) -> tuple[str, str]:
    blocked = [r for r in facts.results if _cand_dict(r).get("blocked")]
    errored = [r for r in facts.results if _cand_dict(r).get("error")]
    failed_pages = [c for c in facts.candidates if _cand_dict(c).get("fetch_error")]
    if facts.matches:
        return result_module.SHOP_FOUND, ""
    if blocked:
        return result_module.SHOP_NOT_SEARCHED, "blocked/CAPTCHA"
    if errored:
        return result_module.SHOP_NOT_SEARCHED, str(_cand_dict(errored[0]).get("error"))
    if failed_pages:
        return result_module.SHOP_NOT_SEARCHED, str(_cand_dict(failed_pages[0]).get("fetch_error"))
    if facts.lane_error:
        return result_module.SHOP_INCOMPLETE, facts.lane_error
    if facts.seconds is not None and facts.seconds > LANE_TIME_LIMIT_SECONDS:
        return (
            result_module.SHOP_TIMED_OUT,
            f"lane took {facts.seconds:.0f} s (limit {LANE_TIME_LIMIT_SECONDS:.0f} s)",
        )
    if facts.unchecked:
        return result_module.SHOP_INCOMPLETE, f"{facts.unchecked} listing(s) could not be checked"
    if facts.possibles:
        return result_module.SHOP_POSSIBLE, f"{facts.possibles} possible listing(s), none confirmed"
    if facts.seen == 0 and not facts.results:
        return result_module.SHOP_NOT_SEARCHED, "the search returned no listings"
    return result_module.SHOP_NOT_FOUND, "searched, nothing confirmed"


def _shop_retryable(facts: _ShopFacts, status: str, nothing_found: bool, retried: set) -> tuple[bool, bool]:
    # A lane that CRASHED is not retried: the same call would crash again.
    # A blocked/errored search IS retryable - new words may
    # reach a shop the first query could not.
    hard_failure = bool(facts.lane_error)
    retryable = (
        status in result_module.UNFINISHED_STATUSES
        or (status == result_module.SHOP_NOT_FOUND and nothing_found)
    ) and not hard_failure and status != result_module.SHOP_FOUND
    # The discovery lane's retry covers every other shop at once.
    retried_here = facts.domain in retried or ("other" in retried and facts.domain not in LANE_FOR_DOMAIN)
    if retried_here:
        # One retry per shop per run: the flag tells the caller there is
        # nothing left to spend on this shop.
        retryable = False
    return retryable, retried_here


def _shop_card(facts: _ShopFacts, status: str, reason: str, retryable: bool, retried_here: bool) -> dict:
    return result_module.shop_entry(
        status=status,
        reason=reason,
        seen=facts.seen,
        dropped=facts.dropped,
        checked=facts.checked,
        matches=facts.matches,
        retried=retried_here,
        retryable=retryable,
        seconds=facts.seconds,
    )


def _discovery_summary(discovery_results):
    rows = [
        r.to_dict() if hasattr(r, "to_dict") else r
        for r in (discovery_results if isinstance(discovery_results, (list, tuple)) else [])
    ]
    blocked = any(r.get("blocked") for r in rows if isinstance(r, dict))
    urls = sum(
        len(r.get("filtered_urls") or r.get("urls") or r.get("raw_urls") or [])
        for r in rows
        if isinstance(r, dict)
    )
    # Every query failed at the search backend (DDG on cooldown, Brave 429,
    # "no result links could be parsed"): no shop was actually looked at, so
    # the card must not claim "searched, nothing confirmed".
    failed = bool(rows) and not urls and all(
        r.get("blocked") or r.get("error") for r in rows if isinstance(r, dict)
    )
    return blocked, failed


def _other_status(*, discovered, other_lane_error, discovery_blocked, discovery_failed, generic_entries):
    if not discovered:
        return result_module.SHOP_NOT_SEARCHED, "discovery is switched off"
    if other_lane_error:
        return result_module.SHOP_INCOMPLETE, other_lane_error
    if discovery_blocked:
        return result_module.SHOP_NOT_SEARCHED, "discovery blocked/CAPTCHA"
    if discovery_failed:
        return result_module.SHOP_NOT_SEARCHED, "the web search failed, so other shops were not searched"
    if any(e["status"] == result_module.SHOP_FOUND for e in generic_entries):
        return result_module.SHOP_FOUND, ""
    if any(e["status"] in result_module.UNFINISHED_STATUSES for e in generic_entries):
        return result_module.SHOP_INCOMPLETE, "some other shops could not be searched"
    if any(e["status"] == result_module.SHOP_POSSIBLE for e in generic_entries):
        return result_module.SHOP_POSSIBLE, "possible listings only"
    return result_module.SHOP_NOT_FOUND, "searched, nothing confirmed"


def _other_entry(*, entries, discovery_results, lane_errors, lane_seconds, retried, nothing_found, discovered):
    generic_entries = [
        e
        for name, e in entries.items()
        if name not in LANE_FOR_DOMAIN.values() and name not in ("flipkart.com", "amazon.in")
    ]
    other_lane_error = next((e for e in (lane_errors or []) if str(e).startswith("other_shops")), "")
    discovery_blocked, discovery_failed = _discovery_summary(discovery_results)
    other_status, other_reason = _other_status(
        discovered=discovered,
        other_lane_error=other_lane_error,
        discovery_blocked=discovery_blocked,
        discovery_failed=discovery_failed,
        generic_entries=generic_entries,
    )
    other_retryable = (
        other_status in result_module.UNFINISHED_STATUSES and discovered and not other_lane_error
    ) or (other_status == result_module.SHOP_NOT_FOUND and nothing_found)
    if "other" in retried:
        other_retryable = False
    return result_module.shop_entry(
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
    """One card per shop. Pure data in, pure data out.

    Rules: blocked/errored search -> not_searched; a listing that could not be
    checked -> incomplete (never not_found); a lane over its time limit ->
    timed_out; at least one possible but no match -> possible; searched
    cleanly and nothing -> not_found."""
    retried = set(retried or ())
    decisions = list(getattr(match_result, "all_decisions", None) or [])
    confirmed = list(getattr(match_result, "confirmed_matches", None) or [])
    possible = list(getattr(match_result, "possible_matches", None) or [])
    skipped = list(getattr(match_result, "skipped", None) or [])

    entries: dict = {}
    for domain in sorted(_shop_domains(generic_candidates)):
        results, candidates = _shop_sources(
            domain,
            flipkart_results,
            flipkart_candidates,
            amazon_results,
            amazon_candidates,
            generic_candidates,
        )
        counts = _shop_counts(domain, candidates, decisions, confirmed, possible, skipped, prefilter_dropped)
        lane = LANE_FOR_DOMAIN.get(domain, "other_shops")
        lane_error = next((e for e in (lane_errors or []) if str(e).startswith(lane)), "")
        facts = _shop_facts(domain, results, candidates, counts, lane_error, lane_seconds)
        status, reason = _shop_status(facts)
        retryable, retried_here = _shop_retryable(facts, status, nothing_found, retried)
        entries[domain] = _shop_card(facts, status, reason, retryable, retried_here)

    # The discovery lane itself, summarised: a crash there is not any one
    # shop's fault, and the page needs one card that says so.
    entries["other"] = _other_entry(
        entries=entries,
        discovery_results=discovery_results,
        lane_errors=lane_errors,
        lane_seconds=lane_seconds,
        retried=retried,
        nothing_found=nothing_found,
        discovered=discovered,
    )
    return entries

def _aggregate_domains(confirmed, possible, skipped):
    matched_domains = {_norm_domain(m.get("source_domain")) for m in confirmed}
    possible_domains = {_norm_domain(m.get("source_domain")) for m in possible if m.get("source_domain")}
    skipped_domains: set = set()
    for url in skipped:
        if isinstance(url, str) and url.startswith("http"):
            domain = _norm_domain(urlparse(url).netloc)
            if domain:
                skipped_domains.add(domain)
    return matched_domains, possible_domains, skipped_domains


def _not_found_and_issues(tried_domains, matched_domains, possible_domains, skipped_domains, search_issues):
    verify_incomplete = sorted((tried_domains - matched_domains) & skipped_domains)
    not_found_on = sorted(tried_domains - matched_domains - skipped_domains - possible_domains)
    for domain in verify_incomplete:
        search_issues.append(f"{domain}: candidates skipped (verify incomplete) - not counted as 'not found'")
    for domain in sorted((tried_domains - matched_domains) & possible_domains):
        search_issues.append(f"{domain}: possibly the same product (not confirmed) - not counted as 'not found'")

    seen_issues: set = set()
    search_issues[:] = [i for i in search_issues if not (i in seen_issues or seen_issues.add(i))]
    return not_found_on


def _price_flags(gem_product_dict, confirmed, warnings):
    gem_price = gem_product_dict.get("gem_price")
    if gem_price is None:
        message = "GeM price not extracted - price-sanity gate is OFF and savings comparison unavailable."
        if message not in warnings:
            warnings.append(message)
    for match in confirmed:
        price = match.get("price_used")
        if gem_price is not None and price is not None:
            match["delta_vs_gem"] = price - gem_price
            match["cheaper_than_gem"] = price < gem_price
        match["bulk"] = _norm_domain(match.get("source_domain") or match.get("shop")) in BULK_MARKETPLACES
    bulk_domains = sorted({_norm_domain(m.get("source_domain")) for m in confirmed if m.get("bulk")})
    for domain in bulk_domains:
        message = f"{domain} is a B2B/bulk marketplace - its price may be wholesale, not retail."
        if message not in warnings:
            warnings.append(message)
    return gem_price


def _comparison(gem_price, gem_product_dict, confirmed):
    priced = [m.get("price_used") for m in confirmed if m.get("price_used") is not None]
    return {
        "gem_price": gem_price,
        "gem_price_type": gem_product_dict.get("gem_price_type", "unknown"),
        "cheapest_match_price": min(priced) if priced else None,
        "savings_vs_gem": (gem_price - min(priced)) if (gem_price is not None and priced) else None,
    }


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

    matched_domains, possible_domains, skipped_domains = _aggregate_domains(confirmed, possible, skipped)
    not_found_on = _not_found_and_issues(
        tried_domains,
        matched_domains,
        possible_domains,
        skipped_domains,
        search_issues,
    )
    gem_price = _price_flags(gem_product_dict, confirmed, warnings)
    comparison = _comparison(gem_price, gem_product_dict, confirmed)

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
    reaches the matcher - there is nothing to check. Returns
    (match_result, dropped_by_prefilter)."""
    usable = [c for c in candidates if _usable(c)]
    kept, dropped = _prefilter_candidates(usable, normalized)
    event = {"shop": name, "seen": len(usable), "dropped": len(dropped), "sent": len(kept)}
    if name == "other_shops":
        by_shop: dict[str, dict] = {}

        def bucket(value):
            domain = _norm_domain(value)
            if not domain:
                return None
            entry = by_shop.setdefault(domain, {"seen": 0, "sent": 0, "dropped": 0})
            return entry

        for candidate in usable:
            entry = bucket(_cand_dict(candidate).get("source_domain"))
            if entry is not None:
                entry["seen"] += 1
        for candidate in kept:
            entry = bucket(_cand_dict(candidate).get("source_domain"))
            if entry is not None:
                entry["sent"] += 1
        for candidate in dropped:
            entry = bucket(_cand_dict(candidate).get("source_domain"))
            if entry is not None:
                entry["dropped"] += 1
        event["by_shop"] = by_shop
    events.emit("filter_finished", **event)
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
    """GEM_PARALLEL=1 first pass: three lanes - Flipkart, Amazon and
    discovery+generic - run at the same time, and each lane verifies its own
    candidates as soon as its search returns, so the LLM work of a fast lane
    overlaps the slow Flipkart lane instead of waiting for every search to
    finish. Returns one (results, candidates, match_result) tuple per lane,
    in that order."""
    lane_seconds = lane_seconds if lane_seconds is not None else {}
    events.emit("stage", name="search_both", label="Searching Flipkart, Amazon and other shops at the same time")
    if DISCOVERY_ENABLED:
        events.emit("stage", name="discovery", label="Looking for other shops")

    def direct_lane(search_fn, *, label, shop, name, label_text, seconds_key):
        """One direct marketplace: search, verify, and (only when nothing was
        confirmed and the early exit left a query unrun) one more query.

        The early exit in search_direct stops as soon as enough relevant cards
        arrive, so the query that would have held the exact listing can stay
        unrun. One extra search per lane is cheap and polite - it is never a
        loop, and it never runs after a confirmed match."""
        started = time.monotonic()
        events.emit("shop_started", shop=shop)
        results, candidates = _search_direct(search_fn, queries, label=label, normalized=normalized)
        lane_match, dropped = _verify_lane(normalized, candidates, match_fn, name=name, label=label_text)
        blocked_or_failed = any(
            getattr(result, "blocked", False) or getattr(result, "error", None) for result in results
        )
        if not lane_match.confirmed_matches and len(results) < len(queries) and not blocked_or_failed:
            tried = {getattr(r, "query", None) for r in results}
            remaining = [q for q in queries if q not in tried]
            if remaining:
                events.emit("search_retry", marketplace=label, query=remaining[0])
                retry_results, retry_candidates = _search_direct(
                    search_fn, remaining[:1], label=label, normalized=normalized
                )
                results = list(results) + list(retry_results)
                seen = {_listing_key(_cand_dict(c).get("url")) for c in candidates}
                fresh = [c for c in retry_candidates if _listing_key(_cand_dict(c).get("url")) not in seen]
                candidates = list(candidates) + fresh
                if fresh:
                    retry_match, retry_dropped = _verify_lane(
                        normalized, fresh, match_fn, name=name, label=label_text
                    )
                    lane_match = _merge_match_results([lane_match, retry_match])
                    dropped = list(dropped) + list(retry_dropped)
        lane_seconds[seconds_key] = time.monotonic() - started
        return results, candidates, lane_match, dropped

    def flipkart_lane():
        return direct_lane(
            flipkart_search_fn,
            label="flipkart", shop="flipkart.com", name="flipkart",
            label_text="the Flipkart listings", seconds_key="flipkart",
        )

    def amazon_lane():
        return direct_lane(
            amazon_search_fn,
            label="amazon", shop="amazon.in", name="amazon",
            label_text="the Amazon listings", seconds_key="amazon",
        )

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


def _lane_value(lane, name):
    return lane.get(name) if isinstance(lane, dict) else getattr(lane, name, None)


def _merge_lane_decisions(lane_results: list):
    kept: list = []
    kept_ids: set = set()
    seen_keys: set = set()
    for lane in lane_results:
        for d in _lane_value(lane, "all_decisions") or []:
            key = _listing_key(d.get("candidate_url")) or f"__noid_{len(kept)}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            kept.append(d)
            kept_ids.add(id(d))
    return kept, kept_ids


def _merge_skipped(lane_results: list) -> list:
    skipped: list[str] = []
    seen_skipped: set = set()
    for lane in lane_results:
        for u in _lane_value(lane, "skipped") or []:
            key = _listing_key(u) or f"__noid_skip_{len(skipped)}"
            if key in seen_skipped:
                continue
            seen_skipped.add(key)
            skipped.append(u)
    return skipped


def _merge_confirmed(lane_results: list, kept_ids: set) -> list:
    confirmed: list = []
    for lane in lane_results:
        # A lane's confirmed entry is the SAME dict object as its all_decisions
        # entry - a duplicate dropped above must not reappear here.
        for d in _lane_value(lane, "confirmed_matches") or []:
            if id(d) in kept_ids:
                confirmed.append(d)
    confirmed.sort(key=lambda d: d.get("price_used") if d.get("price_used") is not None else float("inf"))
    return confirmed


def _merge_possible(lane_results: list, kept_ids: set) -> list:
    possible: list = []
    for lane in lane_results:
        for d in _lane_value(lane, "possible_matches") or []:
            if id(d) in kept_ids:
                possible.append(d)
    possible.sort(
        key=lambda d: (
            d.get("price_used") is None,
            d.get("price_used") if d.get("price_used") is not None else 0.0,
        )
    )
    return possible


def _merge_not_checked(lane_results: list) -> list:
    not_checked: list[str] = []
    seen_not_checked: set = set()
    for lane in lane_results:
        for u in _lane_value(lane, "not_checked") or []:
            key = _listing_key(u) or f"__noid_nc_{len(not_checked)}"
            if key in seen_not_checked:
                continue
            seen_not_checked.add(key)
            not_checked.append(u)
    return not_checked


def _merge_match_results(lane_results: list):
    """Merge the per-lane match batches (GEM_PARALLEL=1). The same listing can
    be checked by two lanes (a Flipkart URL arriving from both the direct
    search and discovery), so decisions are deduped by _listing_key - the
    first lane's copy wins. Confirmed matches are re-sorted cheapest first,
    like the matcher does within one batch."""
    kept, kept_ids = _merge_lane_decisions(lane_results)
    return matcher.MatchBatchResult(
        all_decisions=kept,
        confirmed_matches=_merge_confirmed(lane_results, kept_ids),
        skipped=_merge_skipped(lane_results),
        possible_matches=_merge_possible(lane_results, kept_ids),
        not_checked=_merge_not_checked(lane_results),
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
    """Same for decision rows - a retry must not duplicate a first-pass verdict."""
    return _merge_rows(first, second)


# --- main entry point --------------------------------------------------------------

@dataclass
class RunContext:
    """One run's shared state, carried from phase to phase."""

    gem_url: str
    refresh: bool
    use_cache: bool
    trace_id: str
    trace: dict
    started_monotonic: float = field(default_factory=time.monotonic)
    warnings: list = field(default_factory=list)
    lane_seconds: dict = field(default_factory=dict)
    flipkart_browser: object = None
    flipkart_search_fn: object = None
    match_fn: object = None
    gem_product_dict: dict = field(default_factory=dict)
    normalized: dict = field(default_factory=dict)
    queries: list = field(default_factory=list)
    lane_errors: list = field(default_factory=list)
    lane_mode: bool = False
    prefilter_dropped: list = field(default_factory=list)
    flipkart_results: list = field(default_factory=list)
    flipkart_candidates: list = field(default_factory=list)
    flipkart_match: object = None
    flipkart_dropped: list = field(default_factory=list)
    amazon_results: list = field(default_factory=list)
    amazon_candidates: list = field(default_factory=list)
    amazon_match: object = None
    amazon_dropped: list = field(default_factory=list)
    discovery_results: list = field(default_factory=list)
    generic_candidates: list = field(default_factory=list)
    other_shops_match: object = None
    other_shops_dropped: list = field(default_factory=list)
    search_issues: list = field(default_factory=list)
    tried_domains: set = field(default_factory=set)
    all_candidates: list = field(default_factory=list)
    similar_pool: list = field(default_factory=list)
    match_result: object = None
    final_match_result: object = None
    confirmed: list = field(default_factory=list)
    first_possible: list = field(default_factory=list)
    first_shops: dict = field(default_factory=dict)
    retry_trace: dict = field(default_factory=dict)
    retried_shops: set = field(default_factory=set)
    similar: list = field(default_factory=list)
    result: dict = field(default_factory=dict)


def _cached_result(gem_url: str, refresh: bool, use_cache: bool) -> Optional[dict]:
    if refresh or not use_cache:
        return None
    cached = _cache_get(gem_url)
    if cached is None:
        return None
    result = dict(cached)
    result["from_cache"] = True
    events.emit("cache_hit", gem_url=gem_url)
    _emit_finished(result)
    events.emit("done", state=result.get("state"), from_cache=True, trace_id=result.get("trace_id"))
    return result


def _start_browser(ctx: RunContext) -> None:
    if ctx.flipkart_search_fn is not None:
        return
    # One Chrome for the whole run - every query and the retry - and it
    # starts building NOW, so the 3-8 s startup hides behind the GeM
    # page fetch and the LLM normalize instead of delaying the first
    # search. Tests and the replay tool pass their own search function,
    # so no browser is ever started for them.
    ctx.flipkart_browser = flipkart_scraper.FlipkartBrowser()
    ctx.flipkart_browser.start()
    ctx.flipkart_search_fn = ctx.flipkart_browser.search


def read_gem(ctx: RunContext, extract_fn) -> None:
    events.emit("stage", name="extract", label="Reading the GeM product page")
    gem_product = extract_fn(ctx.gem_url)
    gem_product_dict = gem_product.to_dict() if hasattr(gem_product, "to_dict") else gem_product
    ctx.gem_product_dict = gem_product_dict
    ctx.trace["stages"]["extraction"] = gem_product_dict
    events.emit(
        "gem_product",
        title=gem_product_dict.get("title"),
        price=gem_product_dict.get("gem_price"),
        price_type=gem_product_dict.get("gem_price_type"),
        brand=gem_product_dict.get("brand"),
        model=gem_product_dict.get("model"),
        category=gem_product_dict.get("category_slug"),
        specs=gem_product_dict.get("specs") or {},
        image=gem_product_dict.get("image"),
    )
    events.emit("product_ready", title=gem_product_dict.get("title"), price=gem_product_dict.get("gem_price"))


def understand(ctx: RunContext, normalize_fn) -> None:
    events.emit("stage", name="normalize", label="Working out what the product really is")
    try:
        normalized = normalize_fn(ctx.gem_product_dict)
    except GatewayExhausted as exc:
        # Every LLM allowance is gone for the day. The comparison is
        # still worth running with the GeM title as the identity; the
        # listings simply cannot be verified (reported per lane below).
        _note_llm_offline(ctx.warnings, str(exc))
        normalized = _identity_without_llm(ctx.gem_product_dict)
    ctx.normalized = normalized
    ctx.trace["stages"]["normalization"] = normalized
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
        fallback = (normalized.get("canonical_name") or ctx.gem_product_dict.get("title") or "").strip()
        queries = [fallback] if fallback else [ctx.gem_product_dict.get("category_slug") or "product"]
    ctx.queries = queries

    # verdicts are shared across the lanes of one run - the
    # same listing title + price for the same product is one question.
    if ctx.match_fn is matcher.match_candidates and REUSE_VERDICTS:
        ctx.match_fn = partial(ctx.match_fn, verdict_cache={})

    # Same deal at the matching step: a lane whose LLM call finds every
    # endpoint busy or used up reports its listings as skipped.
    ctx.match_fn = _guarded_match(ctx.match_fn, ctx.warnings)

    if ctx.flipkart_browser is not None:
        # the Flipkart page loop stops early when page 1 already holds
        # enough relevant cards - give it the product's words to judge by.
        ctx.flipkart_search_fn = partial(ctx.flipkart_browser.search, product=prefilter.product_tokens(normalized))


def _run_lanes(ctx: RunContext, amazon_search_fn, discover_fn, generic_scrape_fn) -> None:
    (
        (ctx.flipkart_results, ctx.flipkart_candidates, ctx.flipkart_match, ctx.flipkart_dropped),
        (ctx.amazon_results, ctx.amazon_candidates, ctx.amazon_match, ctx.amazon_dropped),
        (ctx.discovery_results, ctx.generic_candidates, ctx.other_shops_match, ctx.other_shops_dropped),
    ) = _run_first_pass_lanes(
        ctx.flipkart_search_fn, amazon_search_fn, ctx.queries,
        discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn,
        normalized=ctx.normalized, match_fn=ctx.match_fn,
        lane_errors=ctx.lane_errors,
        lane_seconds=ctx.lane_seconds,
    )
    ctx.prefilter_dropped = list(ctx.flipkart_dropped) + list(ctx.amazon_dropped) + list(ctx.other_shops_dropped)
    if _prefilter_top() > 0:
        ctx.trace["stages"]["prefilter"] = {"top": _prefilter_top(), "dropped": ctx.prefilter_dropped}


def _run_sequential_search(ctx: RunContext, amazon_search_fn) -> None:
    events.emit("shop_started", shop="flipkart.com")
    events.emit("shop_started", shop="amazon.in")
    started = time.monotonic()
    (
        ctx.flipkart_results,
        ctx.flipkart_candidates,
        ctx.amazon_results,
        ctx.amazon_candidates,
    ) = _search_marketplaces(
        ctx.flipkart_search_fn, amazon_search_fn, ctx.queries, normalized=ctx.normalized
    )
    ctx.lane_seconds["flipkart"] = time.monotonic() - started
    ctx.lane_seconds["amazon"] = ctx.lane_seconds["flipkart"]
    ctx.discovery_results = ctx.generic_candidates = None


def _run_sequential_discovery(ctx: RunContext, discover_fn, generic_scrape_fn) -> None:
    events.emit("shop_started", shop="other")
    started = time.monotonic()
    if DISCOVERY_ENABLED:
        events.emit("stage", name="discovery", label="Looking for other shops")
        ctx.discovery_results, ctx.generic_candidates = _run_discovery_and_generic_scrape(
            ctx.queries, None, discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn
        )
    else:
        ctx.discovery_results, ctx.generic_candidates = [], []
    ctx.lane_seconds["other_shops"] = time.monotonic() - started
    seen_domains: set = set()
    for candidate in ctx.generic_candidates:
        shop = _norm_domain(_cand_dict(candidate).get("source_domain"))
        if shop and shop not in seen_domains:
            seen_domains.add(shop)
            events.emit("shop_started", shop=shop)


def _note_discovery_health(ctx: RunContext) -> None:
    for dr in ctx.discovery_results:
        d = dr.to_dict() if hasattr(dr, "to_dict") else dr
        if d.get("blocked"):
            ctx.search_issues.append(f"discovery blocked for query {d.get('query')!r}")
        elif d.get("error"):
            ctx.search_issues.append(f"discovery error for query {d.get('query')!r}: {d['error']}")
    for gc in ctx.generic_candidates:
        d = _cand_dict(gc)
        if d.get("fetch_error") and not d.get("raw_page_text") and not d.get("structured_fields"):
            ctx.search_issues.append(f"{d.get('source_domain', '?')}: fetch failed ({d['fetch_error']})")
        elif d.get("source_domain"):
            ctx.tried_domains.add(d["source_domain"])


def _collect_candidates(ctx: RunContext) -> None:
    ctx.all_candidates = _dedup(
        [c for c in list(ctx.flipkart_candidates) + list(ctx.amazon_candidates) + ctx.generic_candidates if _usable(c)]
    )
    # Everything the run ever saw as a usable listing - the similar-products
    # call draws from this, retry results included.
    ctx.similar_pool = list(ctx.all_candidates)


def _first_pass_match(ctx: RunContext) -> None:
    if ctx.lane_mode:
        # Each lane already checked its own listings (and emitted its own
        # match stage); merging keeps one row per listing, first lane wins.
        match_result = _merge_match_results([ctx.flipkart_match, ctx.amazon_match, ctx.other_shops_match])
    else:
        events.emit("stage", name="match", label="Checking which listings are the same product")
        match_input, dropped_now = _prefilter_candidates(ctx.all_candidates, ctx.normalized)
        ctx.prefilter_dropped.extend(dropped_now)
        events.emit("candidates_total", count=len(match_input))
        match_result = ctx.match_fn(ctx.normalized, match_input)
        if _prefilter_top() > 0:
            ctx.trace["stages"]["prefilter"] = {"top": _prefilter_top(), "dropped": ctx.prefilter_dropped}
    ctx.match_result = match_result
    ctx.final_match_result = match_result
    ctx.trace["stages"]["matching"] = match_result.to_dict() if hasattr(match_result, "to_dict") else match_result
    ctx.confirmed = list(match_result.confirmed_matches)
    ctx.first_possible = list(getattr(match_result, "possible_matches", []) or [])


def first_pass(ctx: RunContext, amazon_search_fn, discover_fn, generic_scrape_fn) -> None:
    ctx.prefilter_dropped = []
    ctx.lane_errors = []  # filled only by the parallel lanes (see _run_first_pass_lanes)
    # GEM_STRICT=1 keeps the lanes (searches still run together) but turns
    # the pre-filter off and the verifier back to one listing at a time.
    ctx.lane_mode = PARALLEL_SEARCH or STRICT_MODE
    if ctx.lane_mode:
        # GEM_PARALLEL=1: three lanes - Flipkart, Amazon and discovery+generic
        # - search at the same time, each checking its own listings the moment
        # they arrive (so a fast lane's LLM work overlaps the slow Flipkart lane).
        _run_lanes(ctx, amazon_search_fn, discover_fn, generic_scrape_fn)
    else:
        _run_sequential_search(ctx, amazon_search_fn)
    ctx.trace["stages"]["flipkart_search"] = [
        r.to_dict() if hasattr(r, "to_dict") else r for r in ctx.flipkart_results
    ]
    ctx.trace["stages"]["amazon_search"] = [
        r.to_dict() if hasattr(r, "to_dict") else r for r in ctx.amazon_results
    ]

    # Search health decides who's honestly "searched" vs who failed to scrape.
    ctx.search_issues = list(ctx.lane_errors)
    ctx.tried_domains = set()
    for r in ctx.flipkart_results + ctx.amazon_results:
        _record_search_health(r, ctx.tried_domains, ctx.search_issues)

    # --- first pass: open discovery + generic scrape for everything else ---
    if not ctx.lane_mode:
        _run_sequential_discovery(ctx, discover_fn, generic_scrape_fn)
    ctx.trace["stages"]["discovery"] = [
        d.to_dict() if hasattr(d, "to_dict") else d for d in ctx.discovery_results
    ]
    ctx.trace["stages"]["generic_scrapes"] = [
        c.to_dict() if hasattr(c, "to_dict") else c for c in ctx.generic_candidates
    ]

    _note_discovery_health(ctx)
    _collect_candidates(ctx)
    _first_pass_match(ctx)


def first_result(ctx: RunContext) -> None:
    first_shops = _shop_entries(
        flipkart_results=ctx.flipkart_results,
        amazon_results=ctx.amazon_results,
        discovery_results=ctx.discovery_results,
        flipkart_candidates=ctx.flipkart_candidates,
        amazon_candidates=ctx.amazon_candidates,
        generic_candidates=ctx.generic_candidates,
        match_result=ctx.match_result,
        prefilter_dropped=ctx.prefilter_dropped,
        lane_errors=ctx.lane_errors,
        lane_seconds=ctx.lane_seconds,
        nothing_found=not ctx.confirmed and not ctx.first_possible,
        discovered=bool(DISCOVERY_ENABLED),
    )
    ctx.first_shops = first_shops
    ctx.trace["stages"]["shops"] = first_shops
    shown_result, _, _ = _aggregate(
        gem_url=ctx.gem_url,
        gem_product_dict=ctx.gem_product_dict,
        normalized=ctx.normalized,
        match_result=ctx.match_result,
        shops=first_shops,
        similar=[],
        warnings=ctx.warnings,
        search_issues=ctx.search_issues,
        tried_domains=ctx.tried_domains,
        trace_id=ctx.trace_id,
        state="shown",
    )
    for name, entry in first_shops.items():
        events.emit("shop_finished", shop=name, **entry)
    # Saved before the event so a listener that reads the record (the CLI)
    # never races the write.
    _save_run_record(ctx.gem_url, shown_result, ctx.trace_id, state="shown")
    _emit_finished(shown_result)


def _expand_retry(ctx: RunContext, expand_search_fn) -> dict:
    # expand_search is OPTIONAL logic - if it throws (bad JSON, 429,
    # timeout) the run must survive on its first-pass results. It
    # propagated and destroyed a fully successful run.
    try:
        return expand_search_fn(
            ctx.normalized,
            list(ctx.queries),
            sorted(ctx.tried_domains | set(KNOWN_MARKETPLACE_DOMAINS)),
        )
    except GatewayExhausted as e:
        # a used-up LLM cannot suggest new words, so the
        # retry is skipped and the fact is a warning, not an error.
        ctx.retry_trace["expansion_error"] = f"{type(e).__name__}: {e}"
        ctx.retry_trace["skipped_reason"] = "LLM limits used up"
        _note_llm_offline(ctx.warnings, f"Retry skipped: {e}")
        logger.warning("expand_search found the LLM used up (%s) - skipping retry.", e)
        return {"new_queries": [], "suggested_domains": [], "notes": ""}
    except Exception as e:
        ctx.retry_trace["expansion_error"] = f"{type(e).__name__}: {e}"
        logger.warning("expand_search failed (%s) - skipping retry, keeping first-pass results.", e)
        return {"new_queries": [], "suggested_domains": [], "notes": ""}


def _retry_marketplace_search(ctx: RunContext, retry_queries, amazon_search_fn, *, skip_flipkart, skip_amazon):
    try:
        (
            retry_flipkart_results,
            retry_flipkart_candidates,
            retry_amazon_results,
            retry_amazon_candidates,
        ) = _search_marketplaces(
            ctx.flipkart_search_fn,
            amazon_search_fn,
            retry_queries,
            again=True,
            normalized=ctx.normalized,
            skip_flipkart=skip_flipkart,
            skip_amazon=skip_amazon,
        )
        failed = False
    except Exception as e:  # noqa: BLE001 - the retry must not kill the run
        ctx.retry_trace["search_error"] = f"{type(e).__name__}: {e}"
        logger.warning("Retry search failed (%s) - keeping the first-pass result.", e)
        retry_flipkart_results = retry_flipkart_candidates = []
        retry_amazon_results = retry_amazon_candidates = []
        failed = True
    return (
        retry_flipkart_results,
        retry_flipkart_candidates,
        retry_amazon_results,
        retry_amazon_candidates,
        failed,
    )


def _retry_discovery(ctx: RunContext, retry_queries, retry_domains, discover_fn, generic_scrape_fn, *, retry_other, search_failed):
    if not (DISCOVERY_ENABLED and retry_other and not search_failed):
        return [], []
    try:
        return _run_discovery_and_generic_scrape(
            retry_queries, retry_domains, discover_fn=discover_fn, generic_scrape_fn=generic_scrape_fn
        )
    except Exception as e:  # noqa: BLE001 - the retry must not kill the run
        ctx.retry_trace["discovery_error"] = f"{type(e).__name__}: {e}"
        logger.warning("Retry discovery failed (%s) - keeping the first-pass result.", e)
        return [], []


def _note_retry_generic_health(ctx: RunContext, retry_generic_candidates) -> None:
    for gc in retry_generic_candidates:
        d = _cand_dict(gc)
        if d.get("fetch_error") and not d.get("raw_page_text") and not d.get("structured_fields"):
            ctx.search_issues.append(f"{d.get('source_domain', '?')}: fetch failed on retry ({d['fetch_error']})")
        elif d.get("source_domain"):
            ctx.tried_domains.add(d["source_domain"])


def _reused_retry_decisions(ctx: RunContext, retry_pool):
    reused_decisions: list = []
    if not REUSE_VERDICTS:
        return retry_pool, reused_decisions
    reused_decisions = list(ctx.match_result.all_decisions) if hasattr(ctx.match_result, "all_decisions") else []
    decided_keys = {_listing_key(d.get("candidate_url")) for d in reused_decisions}
    retry_pool = [c for c in retry_pool if _listing_key(_cand_dict(c).get("url")) not in decided_keys]
    ctx.retry_trace["reused_verdicts"] = len(reused_decisions)
    return retry_pool, reused_decisions


def _retry_match(ctx: RunContext, retry_candidates: list) -> None:
    # Re-match EVERYTHING still in play: first-pass candidates
    # (including any skipped by a transient LLM failure) plus the
    # retry's new ones, because the retry keeps pass-1 candidates
    # entirely.
    retry_pool = _dedup([c for c in list(ctx.all_candidates) + retry_candidates if _usable(c)])
    ctx.similar_pool.extend(retry_candidates)
    retry_pool, dropped_now = _prefilter_candidates(retry_pool, ctx.normalized)
    ctx.prefilter_dropped.extend(dropped_now)
    if _prefilter_top() > 0:
        ctx.trace["stages"]["prefilter"] = {"top": _prefilter_top(), "dropped": ctx.prefilter_dropped}

    # GEM_REUSE_VERDICTS: the first pass already asked the LLM about
    # these listings and the answers will not change - only ask about
    # what is genuinely new. A listing whose first check was SKIPPED
    # never got a verdict, so it stays in the pool and is asked again.
    retry_pool, reused_decisions = _reused_retry_decisions(ctx, retry_pool)

    events.emit("stage", name="match_retry", label="Checking the new listings")
    if retry_pool:
        retry_match_result = ctx.match_fn(ctx.normalized, retry_pool)
    else:
        retry_match_result = matcher.MatchBatchResult()
    if reused_decisions:
        # final all_decisions must still show what happened with the
        # reused listings - only the confirmed/skipped come from the
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
    ctx.retry_trace["matching"] = (
        retry_match_result.to_dict() if hasattr(retry_match_result, "to_dict") else retry_match_result
    )

    # keep what the first pass already confirmed - a
    # retry caused by one unfinished shop must not erase another
    # shop's match.
    ctx.final_match_result = matcher.MatchBatchResult(
        all_decisions=_merge_decisions(
            list(getattr(retry_match_result, "all_decisions", []) or []),
            list(getattr(ctx.match_result, "all_decisions", []) or []),
        ),
        confirmed_matches=_merge_rows(ctx.match_result.confirmed_matches, retry_match_result.confirmed_matches),
        skipped=list(retry_match_result.skipped),
        possible_matches=_merge_rows(
            getattr(ctx.match_result, "possible_matches", []) or [],
            getattr(retry_match_result, "possible_matches", []) or [],
        ),
        not_checked=list(getattr(retry_match_result, "not_checked", []) or []),
    )
    ctx.confirmed = list(ctx.final_match_result.confirmed_matches)


def _retry_execute(
    ctx: RunContext,
    retry_queries,
    retry_domains,
    amazon_search_fn,
    discover_fn,
    generic_scrape_fn,
) -> None:
    retried_shops = ctx.retried_shops
    # Re-search the two DIRECT scrapers with the expanded queries -
    # expansion feeds both the direct scrapers and discovery,
    # so a bad primary query on the working marketplaces was never
    # retried with the LLM's better phrasing. All retry_queries are
    # eligible, not just [0].
    # shops that are not retryable (found/possible, or a
    # lane that crashed) are skipped - they already answered. A
    # blocked shop IS retried with the new words; the
    # block is recorded so a repeat is visible in the trace.
    skip_flipkart = "flipkart.com" not in retried_shops
    skip_amazon = "amazon.in" not in retried_shops
    retry_other = "other" in retried_shops or bool(
        retried_shops - {"flipkart.com", "amazon.in", "other"}
    )
    (
        retry_flipkart_results,
        retry_flipkart_candidates,
        retry_amazon_results,
        retry_amazon_candidates,
        retry_search_failed,
    ) = _retry_marketplace_search(
        ctx,
        retry_queries,
        amazon_search_fn,
        skip_flipkart=skip_flipkart,
        skip_amazon=skip_amazon,
    )
    ctx.retry_trace["blocked_first_pass"] = [
        name for name, skipped in (("flipkart", skip_flipkart), ("amazon", skip_amazon)) if skipped
    ]
    ctx.trace["stages"]["flipkart_search_retry"] = [
        r.to_dict() if hasattr(r, "to_dict") else r for r in retry_flipkart_results
    ]
    ctx.trace["stages"]["amazon_search_retry"] = [
        r.to_dict() if hasattr(r, "to_dict") else r for r in retry_amazon_results
    ]
    for r in retry_flipkart_results + retry_amazon_results:
        _record_search_health(r, ctx.tried_domains, ctx.search_issues)

    retry_discovery_results, retry_generic_candidates = _retry_discovery(
        ctx,
        retry_queries,
        retry_domains,
        discover_fn,
        generic_scrape_fn,
        retry_other=retry_other,
        search_failed=retry_search_failed,
    )
    ctx.retry_trace["discovery"] = [
        d.to_dict() if hasattr(d, "to_dict") else d for d in retry_discovery_results
    ]
    ctx.retry_trace["generic_scrapes"] = [
        c.to_dict() if hasattr(c, "to_dict") else c for c in retry_generic_candidates
    ]
    _note_retry_generic_health(ctx, retry_generic_candidates)

    _retry_match(
        ctx,
        list(retry_flipkart_candidates) + list(retry_amazon_candidates) + retry_generic_candidates,
    )


def _retry_plan(ctx: RunContext, retry_shops, expand_search_fn, amazon_search_fn, discover_fn, generic_scrape_fn) -> None:
    ctx.retry_trace["state"] = "updating"
    ctx.retry_trace["shops"] = sorted(retry_shops)
    events.emit("retry_started", shops=sorted(retry_shops))
    events.emit("stage", name="retry", label="Nothing matched yet, asking the LLM for better search words")
    expansion = _expand_retry(ctx, expand_search_fn)
    ctx.retry_trace["expansion"] = expansion
    events.emit("retry_plan", new_queries=_str_list(expansion.get("new_queries")), notes=expansion.get("notes", ""))

    retry_queries = retry_module.fresh_queries(expansion, ctx.queries)
    # Strict-mode Indian filter: the prompt asks for Indian domains but
    # doesn't enforce it - a foreign suggestion (bestbuy.com) must not
    # reopen open-mode .com discovery.
    retry_domains = [d for d in _str_list(expansion.get("suggested_domains")) if discovery._is_indian_retail(d)] or None  # None -> open discovery

    if not retry_queries:
        ctx.retry_trace["skipped_reason"] = "expand_search returned no new queries to try"
        return
    # Only now is a retry really happening: new words exist, so the
    # cards can honestly say `retried`.
    ctx.retried_shops = set(retry_shops)
    ctx.retry_trace["triggered"] = True
    _retry_execute(
        ctx,
        retry_queries,
        retry_domains,
        amazon_search_fn,
        discover_fn,
        generic_scrape_fn,
    )


def retry_pass(ctx: RunContext, expand_search_fn, amazon_search_fn, discover_fn, generic_scrape_fn) -> None:
    ctx.retry_trace = {"triggered": False}
    ctx.retried_shops = set()
    retry_shops = retry_module.retryable_shops(
        ctx.first_shops, nothing_found=not ctx.confirmed and not ctx.first_possible
    )
    if ctx.first_shops and RETRY_ENABLED and retry_shops:
        _retry_plan(ctx, retry_shops, expand_search_fn, amazon_search_fn, discover_fn, generic_scrape_fn)
    ctx.trace["stages"]["retry"] = ctx.retry_trace


def similar_pass(ctx: RunContext) -> None:
    final_possible = list(getattr(ctx.final_match_result, "possible_matches", []) or [])
    # Only when the run found nothing at all - not one confirmed match and
    # not one possible one. A "similar" product is a different thing at a
    # different price, so it never feeds the cheapest/savings arithmetic.
    if not ctx.confirmed and not final_possible and ctx.similar_pool:
        ctx.similar = _similar_products(ctx.normalized, ctx.similar_pool, ctx.warnings)
    ctx.trace["stages"]["similar"] = ctx.similar


def _summary_line(ctx: RunContext) -> str:
    calls = [event for event in ctx.trace.get("events") or [] if event.get("type") == "llm_call"]
    failures = sum(1 for event in calls if not event.get("ok"))
    seconds = time.monotonic() - ctx.started_monotonic
    return (
        f"Run summary: {seconds:.1f} s, {len(ctx.confirmed)} match(es), "
        f"{len(calls)} LLM call(s), {failures} LLM failure(s)."
    )


def finish(ctx: RunContext) -> None:
    final_possible = list(getattr(ctx.final_match_result, "possible_matches", []) or [])
    final_shops = _shop_entries(
        flipkart_results=ctx.flipkart_results,
        amazon_results=ctx.amazon_results,
        discovery_results=ctx.discovery_results,
        flipkart_candidates=ctx.flipkart_candidates,
        amazon_candidates=ctx.amazon_candidates,
        generic_candidates=ctx.generic_candidates,
        match_result=ctx.final_match_result,
        prefilter_dropped=ctx.prefilter_dropped,
        lane_errors=ctx.lane_errors,
        lane_seconds=ctx.lane_seconds,
        nothing_found=not ctx.confirmed and not final_possible,
        retried=ctx.retried_shops if ctx.retry_trace["triggered"] else set(),
        discovered=bool(DISCOVERY_ENABLED),
    )
    ctx.trace["stages"]["shops"] = final_shops

    # --- comparison arithmetic - the actual point of the project ----------------------
    result, confirmed, final_possible = _aggregate(
        gem_url=ctx.gem_url,
        gem_product_dict=ctx.gem_product_dict,
        normalized=ctx.normalized,
        match_result=ctx.final_match_result,
        shops=final_shops,
        similar=ctx.similar,
        warnings=ctx.warnings,
        search_issues=ctx.search_issues,
        tried_domains=ctx.tried_domains,
        trace_id=ctx.trace_id,
        state="done",
    )
    ctx.result = result
    ctx.confirmed = confirmed
    ctx.trace["final_result"] = {
        "matches": confirmed,
        "possible_matches": final_possible,
        "similar": ctx.similar,
        "not_found_on": result.get("not_found_on"),
        "search_issues": ctx.search_issues,
    }

    if ctx.similar:
        events.emit("similar_ready", similar=ctx.similar)
    for name in sorted(ctx.retried_shops if ctx.retry_trace["triggered"] else ()):
        entry = final_shops.get(name)
        if entry:
            events.emit("shop_updated", shop=name, **entry)
    events.emit("run_updated", result=result)
    events.emit("done", state="done", trace_id=ctx.trace_id)
    ctx.trace["events"] = events.snapshot()
    _write_trace(ctx.trace)
    _save_run_record(ctx.gem_url, result, ctx.trace_id, state="done")
    if not ctx.use_cache:
        logger.info("Not caching this run: cache disabled (demo).")
    elif ctx.search_issues:
        logger.info("Not caching this run: %d search issue(s) reported.", len(ctx.search_issues))
    else:
        _cache_set(ctx.gem_url, result)
    logger.info(_summary_line(ctx))


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
    cached = _cached_result(gem_url, refresh, use_cache)
    if cached is not None:
        return cached

    trace_id = uuid.uuid4().hex[:16]
    ctx = RunContext(
        gem_url=gem_url,
        refresh=refresh,
        use_cache=use_cache,
        trace_id=trace_id,
        trace={
            "trace_id": trace_id,
            "result_version": RESULT_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "gem_url": gem_url,
            "stages": {},
        },
        flipkart_search_fn=flipkart_search_fn,
        match_fn=match_fn,
    )
    try:
        _start_browser(ctx)
        read_gem(ctx, extract_fn)
        understand(ctx, normalize_fn)
        first_pass(ctx, amazon_search_fn, discover_fn, generic_scrape_fn)
        first_result(ctx)
        retry_pass(ctx, expand_search_fn, amazon_search_fn, discover_fn, generic_scrape_fn)
        similar_pass(ctx)
        finish(ctx)
        return ctx.result
    except Exception as exc:
        # Any failure after the trace dict exists: write a partial trace first,
        # then re-raise the same exception so callers still see the error.
        ctx.trace["error"] = f"{type(exc).__name__}: {exc}"
        ctx.trace["events"] = events.snapshot()
        _write_trace(ctx.trace)
        raise
    finally:
        # The run owns its Chrome: every exit path (success, failure, early
        # return) goes through here, so it is quit exactly once.
        if ctx.flipkart_browser is not None:
            ctx.flipkart_browser.close()


def run_pipeline(gem_url: str, refresh: bool = False, *, on_event=None, **overrides) -> dict:
    """Run the whole comparison for one GeM URL.

    on_event, if given, is called with one small dict for every progress
    event (see core/events.py and docs/api.md). The keyword overrides name
    the stage functions and exist for tests."""
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
