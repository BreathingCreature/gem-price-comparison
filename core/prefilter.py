"""
Part 6 helper — the cheap pre-LLM filter (GEM_PREFILTER_TOP).

A search page often returns 20-40 listings and most of them obviously are
not the product (accessories, other models, pads for a mouse). Each one
would cost one LLM call and be rejected anyway. This module ranks each
shop's titled listings by how much their words overlap the product's words
(canonical name, brand, model, key identifiers — Jaccard similarity) and
keeps only the best N per shop.

Two deliberate choices:
  - Listings with no title are ALWAYS kept — there is nothing to compare,
    and an untitled page could still be the right one.
  - Whatever gets dropped is returned so the pipeline can save it in the
    trace: the filter must be auditable, not a black box (kit 3's
    eval/analyze_misses.py reads the trace to check whether a real match
    was cut, and the safe N is chosen from that, not by guessing).
"""
from __future__ import annotations

import re

from core import events


def _tokens(text: str) -> set[str]:
    """Words worth comparing: lowercase, split on anything non-alphanumeric,
    single characters dropped (they match everything and mean nothing)."""
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) > 1}


def product_tokens(normalized: dict) -> set[str]:
    """The product's own words. key_identifiers sometimes arrives as a bare
    string from the LLM — iterating that string would add one LETTER per
    token, so it is normalized to a list first."""
    parts: list[str] = []
    for key in ("canonical_name", "brand", "model"):
        value = normalized.get(key) if isinstance(normalized, dict) else None
        if value:
            parts.append(str(value))
    identifiers = normalized.get("key_identifiers") if isinstance(normalized, dict) else None
    if isinstance(identifiers, str):
        identifiers = [identifiers]
    if isinstance(identifiers, list):
        parts.extend(str(v) for v in identifiers if v)
    return _tokens(" ".join(parts))


def similarity(title: str, product: set[str]) -> float:
    """Jaccard overlap between the title's words and the product's words.
    A mouse pad next to a mouse shares 'mouse' and lands near 0.1; the same
    mouse shares brand + model + kind and lands near 0.8."""
    words = _tokens(title)
    if not words or not product:
        return 0.0
    return len(words & product) / len(words | product)


# C6/C7 early-exit gates share one idea: a listing is "relevant" when its
# title's word overlap with the product is at least this much. The example
# above is the calibration — a pad lands near 0.1, the same mouse near 0.8.
RELEVANT_MIN_SCORE = 0.3

# C6: enough relevant cards on a page mean the next Flipkart page cannot add
# much — stop paging there (10-30 s saved on easy products).
ENOUGH_RELEVANT = 5


def relevant_title(title: str, product: set[str]) -> bool:
    """True when a listing title is close enough to the product to count
    toward an early-exit (C6/C7). An empty title counts — there is nothing
    to compare, the same rule filter_candidates uses."""
    if not (title or "").strip():
        return True
    return similarity(title, product) >= RELEVANT_MIN_SCORE


def relevant_candidate(candidate, product: set[str]) -> bool:
    """relevant_title for a CandidateResult or its to_dict() form."""
    d = candidate.to_dict() if hasattr(candidate, "to_dict") else candidate
    fields = d.get("structured_fields")
    title = str(fields.get("title") or "") if isinstance(fields, dict) else ""
    return relevant_title(title, product)


def filter_candidates(candidates: list, normalized: dict, top: int) -> tuple[list, list]:
    """Keep the best `top` titled listings per source_domain, ranked by
    title similarity; untitled listings are always kept. Returns
    (kept, dropped) — kept keeps the candidates' original order, dropped is
    a list of small dicts (url, source_domain, title, score) for the trace.
    top <= 0 means off: everything is kept."""
    if top <= 0 or not candidates:
        return list(candidates), []

    product = product_tokens(normalized)
    titles: list[str] = []
    by_shop: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidates):
        d = candidate.to_dict() if hasattr(candidate, "to_dict") else candidate
        fields = d.get("structured_fields")
        title = str(fields.get("title") or "").strip() if isinstance(fields, dict) else ""
        titles.append(title)
        by_shop.setdefault(str(d.get("source_domain") or "?"), []).append(index)

    keep: set[int] = set()
    dropped: list[dict] = []
    for indexes in by_shop.values():
        titled = [i for i in indexes if titles[i]]
        keep.update(i for i in indexes if not titles[i])  # no title -> nothing to compare, always kept
        if len(titled) <= top:
            keep.update(titled)
            continue
        scored = sorted(((similarity(titles[i], product), i) for i in titled), key=lambda pair: (-pair[0], pair[1]))
        keep.update(i for _, i in scored[:top])
        for score, i in scored[top:]:
            d = candidates[i].to_dict() if hasattr(candidates[i], "to_dict") else candidates[i]
            dropped.append({
                "url": d.get("url"),
                "source_domain": d.get("source_domain"),
                "title": titles[i],
                "score": round(score, 3),
            })

    events.emit("prefilter_finished", kept=len(keep), dropped=len(dropped), top=top)
    return [c for i, c in enumerate(candidates) if i in keep], dropped
