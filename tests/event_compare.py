"""Compare event streams without depending on parallel lane interleaving.

The shipped pipeline runs its lanes in parallel. A golden list of every event
type is therefore only stable when PARALLEL_SEARCH is forced off. This helper
keeps the parts that must hold in any thread order: first and last events,
counts, barrier events, and the per-lane sequence of events.
"""
from __future__ import annotations

from collections import Counter

BARRIERS = (
    "run_started", "gem_product", "normalized", "run_finished",
    "retry_started", "run_updated", "similar_ready", "done",
)

SHOP_KEYS = ("shop", "lane", "marketplace", "domain")


def lane_of(event: dict) -> str:
    for key in SHOP_KEYS:
        value = event.get(key)
        if value:
            return str(value)
    return "-"


def canonical(events) -> dict:
    events = list(events or [])
    types = [event.get("type") for event in events]
    lanes: dict[str, list] = {}
    for event in events:
        lane = lane_of(event)
        if lane != "-":
            lanes.setdefault(lane, []).append(event.get("type"))
    return {
        "first": types[0] if types else None,
        "last": types[-1] if types else None,
        "counts": sorted(Counter(types).items()),
        "barriers": [name for name in types if name in BARRIERS],
        "lanes": lanes,
    }
