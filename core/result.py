"""Result v2: one place that builds what a finished run hands to the world.

The old pipeline result was assembled inline, which made every honesty rule
("a shop with matches is found", "no price means no cheapest") a comment in
the middle of a 900-line function. This module builds the whole object from
its parts and enforces the rules once, so the CLI, the API, the web page and
the evaluator all read the same shape.

Shape (docs/api.md, spec/08_INTERFACES.md §2):
    result_version, run_id, trace_id, checked_at, from_cache, state,
    gem {url,title,canonical_name,brand,model,price{low,high,kind},described_by},
    shops {domain: {status,reason,seen,dropped,checked,matches,retried,retryable,seconds}},
    matches[]  (cheapest first, out-of-stock last),
    possible_matches[] (each with `why`),
    similar[]  (never used for cheapest),
    cheapest {shop,title,price,url,saving_vs_gem} | null,
    compared_with [], warnings []

The old top-level keys (`gem_product`, `not_found_on`, `search_issues`,
`skipped`, `comparison`) stay for one release because the CLI, the evaluator
and the current page still read them.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.config import RESULT_VERSION

SHOP_FOUND = "found"
SHOP_POSSIBLE = "possible"
SHOP_NOT_FOUND = "not_found"
SHOP_NOT_SEARCHED = "not_searched"
SHOP_INCOMPLETE = "incomplete"
SHOP_TIMED_OUT = "timed_out"

SHOP_STATUSES = (
    SHOP_FOUND,
    SHOP_POSSIBLE,
    SHOP_NOT_FOUND,
    SHOP_NOT_SEARCHED,
    SHOP_INCOMPLETE,
    SHOP_TIMED_OUT,
)

# Statuses that mean "we did not get a clean look at this shop". A result must
# never claim "nothing was found" while one of these is in the list.
UNFINISHED_STATUSES = (SHOP_NOT_SEARCHED, SHOP_INCOMPLETE, SHOP_TIMED_OUT)


def shop_entry(
    *,
    status: str,
    reason: str = "",
    seen: int = 0,
    dropped: int = 0,
    checked: int = 0,
    matches: int = 0,
    retried: bool = False,
    retryable: bool = False,
    seconds: float | None = None,
) -> dict:
    """One shop's card data. `reason` is required for every status except
    `found`; callers fill it, and build_result refuses to claim otherwise."""
    return {
        "status": status,
        "reason": reason,
        "seen": int(seen),
        "dropped": int(dropped),
        "checked": int(checked),
        "matches": int(matches),
        "retried": bool(retried),
        "retryable": bool(retryable),
        "seconds": round(float(seconds), 2) if seconds is not None else None,
    }


def _price(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = "".join(ch for ch in value if ch.isdigit() or ch == ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    return None


def _match_view(decision: dict, *, possible: bool = False) -> dict:
    """The v2 row for one verdict, keeping the old keys alongside it."""
    row = dict(decision)
    fields = decision.get("structured_fields") if isinstance(decision.get("structured_fields"), dict) else {}
    row["shop"] = decision.get("source_domain") or decision.get("shop") or ""
    row["title"] = decision.get("extracted_title") or fields.get("title") or decision.get("title") or ""
    row["url"] = decision.get("candidate_url") or decision.get("url") or ""
    row["price"] = decision.get("price_used")
    row["in_stock"] = decision.get("in_stock")
    if row["in_stock"] is None:
        row["in_stock"] = True  # unknown is not a reason to hide a confirmed match
    row["confidence"] = decision.get("confidence")
    row["reason"] = decision.get("reason") or ""
    if possible:
        row["why"] = decision.get("why") or decision.get("reason") or "possible"
    return row


def _sort_matches(rows: list[dict]) -> list[dict]:
    """Cheapest first; a missing price last; out-of-stock after every in-stock
    listing (a cheaper out-of-stock listing is not a cheaper listing)."""
    return sorted(
        rows,
        key=lambda row: (
            not row.get("in_stock", True),
            _price(row.get("price")) is None,
            _price(row.get("price")) or 0.0,
            -(float(row.get("confidence") or 0.0)),
        ),
    )


def build_result(
    *,
    gem_url: str,
    gem_product: dict,
    normalized: dict | None,
    shops: dict | None,
    matches: list | None,
    possible_matches: list | None = None,
    similar: list | None = None,
    warnings: list | None = None,
    search_issues: list | None = None,
    not_found_on: list | None = None,
    skipped: list | None = None,
    comparison: dict | None = None,
    state: str = "done",
    from_cache: bool = False,
    trace_id: str | None = None,
    run_id: str | None = None,
    checked_at: str | None = None,
    described_by: str | None = None,
) -> dict:
    gem_product = dict(gem_product or {})
    normalized = normalized or {}
    warnings = list(warnings or [])
    search_issues = list(search_issues or [])
    not_found_on = list(not_found_on or [])
    skipped = list(skipped or [])
    comparison = dict(comparison or {})
    shops = {str(name): dict(entry) for name, entry in (shops or {}).items()}

    match_rows = [_match_view(d) for d in (matches or [])]
    possible_rows = [_match_view(d, possible=True) for d in (possible_matches or [])]
    similar_rows = sorted(
        (dict(row) for row in (similar or [])),
        key=lambda row: (_price(row.get("price")) is None, _price(row.get("price")) or 0.0),
    )

    matched_shops = {row["shop"] for row in match_rows}
    possible_shops = {row["shop"] for row in possible_rows}

    # Honesty check 1: a shop cannot be "found" with zero matches.
    for name, entry in shops.items():
        entry.setdefault("status", SHOP_NOT_FOUND)
        entry.setdefault("reason", "")
        if entry["status"] == SHOP_FOUND and name not in matched_shops:
            entry["status"] = SHOP_POSSIBLE if name in possible_shops else SHOP_NOT_FOUND
            entry["reason"] = entry.get("reason") or "no confirmed match in the final result"
        entry["matches"] = sum(1 for row in match_rows if row["shop"] == name)
        if entry["status"] != SHOP_FOUND and not entry.get("reason"):
            entry["reason"] = "searched, nothing confirmed"

    # Honesty check 2: cheapest needs a priced, in-stock match and a GeM price.
    sorted_rows = _sort_matches(match_rows)
    gem_price = _price(gem_product.get("gem_price"))
    priced_in_stock = [
        row for row in sorted_rows if row.get("in_stock", True) and _price(row.get("price")) is not None
    ]
    cheapest = None
    if gem_price is not None and priced_in_stock:
        best = priced_in_stock[0]  # sorted_rows is cheapest-first, in-stock first
        best_price = _price(best.get("price"))
        cheapest = {
            "shop": best["shop"],
            "title": best["title"],
            "price": best_price,
            "url": best["url"],
            "saving_vs_gem": round(gem_price - best_price, 2),
        }
        comparison = dict(comparison)
        comparison.setdefault("cheapest_match_price", best_price)
        comparison.setdefault("savings_vs_gem", cheapest["saving_vs_gem"])

    # Honesty check 3: never say "nothing found" while a shop was not looked at.
    for name, entry in shops.items():
        if entry.get("status") in UNFINISHED_STATUSES:
            reason = entry.get("reason") or entry["status"]
            line = f"{name}: {entry['status']} ({reason})"
            if line not in warnings:
                warnings.append(line)

    compared_with = sorted(
        name
        for name, entry in shops.items()
        if entry.get("status") in (SHOP_FOUND, SHOP_POSSIBLE, SHOP_NOT_FOUND)
    )

    gem_view = {
        "url": gem_url or gem_product.get("url"),
        "title": gem_product.get("title"),
        "canonical_name": normalized.get("canonical_name") or gem_product.get("title"),
        "brand": normalized.get("brand") or gem_product.get("brand"),
        "model": normalized.get("model") or gem_product.get("model"),
        "price": gem_product.get("price")
        or {"low": gem_price, "high": gem_price, "kind": gem_product.get("gem_price_type", "unknown")},
        "described_by": described_by or normalized.get("described_by") or "llm",
    }

    return {
        # --- v2 ---
        "result_version": RESULT_VERSION,
        "run_id": run_id or trace_id or "",
        "trace_id": trace_id or "",
        "checked_at": checked_at or datetime.now(timezone.utc).isoformat(),
        "from_cache": bool(from_cache),
        "state": state,
        "gem": gem_view,
        "shops": shops,
        "matches": _sort_matches(match_rows),
        "possible_matches": possible_rows,
        "similar": similar_rows,
        "cheapest": cheapest,
        "compared_with": compared_with,
        "warnings": warnings,
        # --- kept for one release (CLI, evaluator, current page) ---
        "gem_product": gem_product,
        "not_found_on": not_found_on,
        "search_issues": search_issues,
        "skipped": skipped,
        "comparison": comparison,
    }
