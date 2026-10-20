"""Stage 7.4 helpers: which shops to retry, and what to search for.

The retry itself (a search plus verify per shop) belongs to the pipeline; the
decisions live here so they can be tested without one.
"""
from __future__ import annotations

from core.result import SHOP_INCOMPLETE, SHOP_NOT_SEARCHED, SHOP_NOT_FOUND, SHOP_TIMED_OUT

# Retry is for shops we did not get a clean answer from. `not_found` is also
# retried when the whole round found nothing (a different phrasing may work).
# Exactly one retry per shop is allowed; the `retried` flag on the card is what
# enforces it.
def retryable_shops(shops: dict, *, nothing_found: bool) -> list[str]:
    """Shop names worth one retry, in card order. A shop already retried is
    never retried again in the same run, and the card's own `retryable` flag
    wins (a blocked or crashed search is not a retry candidate)."""
    picked = []
    for name, entry in (shops or {}).items():
        if entry.get("retried") or not entry.get("retryable", True):
            continue
        status = entry.get("status")
        if status in (SHOP_NOT_SEARCHED, SHOP_INCOMPLETE, SHOP_TIMED_OUT):
            picked.append(name)
        elif status == SHOP_NOT_FOUND and nothing_found:
            picked.append(name)
    return picked


def fresh_queries(expansion: dict, tried_queries: list[str]) -> list[str]:
    """The new words worth trying: stripped, de-duplicated, not already used.
    An LLM that repeats the first-pass queries contributes nothing."""
    fresh: list[str] = []
    for query in (expansion or {}).get("new_queries") or []:
        text = str(query).strip()
        if text and text not in tried_queries and text not in fresh:
            fresh.append(text)
    return fresh
