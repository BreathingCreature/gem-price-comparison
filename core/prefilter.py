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


def _compact(text: str) -> str:
    """Lowercase letters and digits only: "M-650" and "m 650" both become "m650"."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# Brand names that really mean "no brand". A product with one of these (or
# with a brand that is only a guess) must not be punished for it.
NO_BRAND_WORDS = {"", "generic", "na", "n/a", "not applicable", "unbranded", "no brand", "none", "other", "others"}

# Short forms shops and GeM use for the same brand. Add more here when a
# real product needs it (keys and values are lowercase words).
BRAND_ALIASES = {
    "hp": ["hewlett", "packard"],
    "mi": ["xiaomi"],
    "redmi": ["xiaomi"],
    "poco": ["xiaomi"],
    "oneplus": ["one", "plus"],
    "tplink": ["tp", "link"],
    "wd": ["western", "digital"],
}


class ProductWords(set):
    """The product's words (a normal set, so older code keeps working) plus
    what the better scoring needs:
      weights     word -> how much it counts (model 3, brand 2, identifiers 2, others 1)
      model_keys  the model codes without spaces or dashes ("m650")
      brand_words the brand's words plus its aliases; empty when the brand is
                  missing, generic, or not part of the product name"""

    weights: dict
    model_keys: list
    brand_words: set


def product_tokens(normalized: dict) -> ProductWords:
    """The product's own words. key_identifiers sometimes arrives as a bare
    string from the LLM — iterating that string would add one LETTER per
    token, so it is normalized to a list first."""
    data = normalized if isinstance(normalized, dict) else {}
    weights: dict = {}

    def add(text, weight):
        for word in _tokens(str(text or "")):
            weights[word] = max(weights.get(word, 0), weight)

    add(data.get("canonical_name"), 1)

    model_text = str(data.get("model") or "").strip()
    add(model_text, 3)
    model_keys = []
    model_key = _compact(model_text)
    # A model code needs a digit and 3+ characters, or "x" would match everything.
    if len(model_key) >= 3 and any(ch.isdigit() for ch in model_key):
        model_keys.append(model_key)
        weights[model_key] = max(weights.get(model_key, 0), 3)

    identifiers = data.get("key_identifiers")
    if isinstance(identifiers, str):
        identifiers = [identifiers]
    if isinstance(identifiers, list):
        for item in identifiers:
            add(item, 2)

    brand_text = str(data.get("brand") or "").strip()
    brand_words: set = set()
    brand_tokens = _tokens(brand_text)
    name_tokens = _tokens(str(data.get("canonical_name") or ""))
    if brand_text.lower() not in NO_BRAND_WORDS and brand_tokens and brand_tokens <= name_tokens:
        add(brand_text, 2)
        brand_words = set(brand_tokens)
        for word in list(brand_tokens):
            brand_words.update(BRAND_ALIASES.get(word, []))

    product = ProductWords(weights)
    product.weights = weights
    product.model_keys = model_keys
    product.brand_words = brand_words
    return product


def _weights(product) -> dict:
    """The word weights of a ProductWords, or weight 1 for a plain set of words."""
    weights = getattr(product, "weights", None)
    return weights if weights else {word: 1 for word in product}


def similarity(title: str, product) -> float:
    """How much of the PRODUCT's words (weighted) the title contains, 0 to 1.
    A long title does not lower the score, because only the product's words
    are counted, not the title's extra words."""
    words = _tokens(title)
    weights = _weights(product)
    if not words or not weights:
        return 0.0
    compact = _compact(title)
    total = sum(weights.values())
    found = 0.0
    for word, weight in weights.items():
        if word in words or (word in getattr(product, "model_keys", []) and word in compact):
            found += weight
    return found / total


def tier(title: str, product) -> int:
    """1 = the title contains the model code, 2 = it contains the brand,
    3 = neither (or nothing to check). Lower is better."""
    compact = _compact(title)
    for key in getattr(product, "model_keys", []):
        if key in compact:
            return 1
    brand_words = getattr(product, "brand_words", None)
    if brand_words and (_tokens(title) & brand_words):
        return 2
    return 3


# C6/C7 early-exit gates share one idea: a listing is "relevant" when the
# share of the product's words in its title is at least this much. A mouse pad
# next to a mouse scores near 0.15; the same mouse scores near 1.0.
RELEVANT_MIN_SCORE = 0.5

# C6: enough relevant cards on a page mean the next Flipkart page cannot add
# much — stop paging there (10-30 s saved on easy products).
ENOUGH_RELEVANT = 5

# How many model-code hits one shop may keep beyond N (times N). Pages full of
# accessories "for Logitech M650" must not send everything to the LLM.
MODEL_HIT_CAP_FACTOR = 3


def relevant_title(title: str, product) -> bool:
    """True when a listing title is close enough to the product to count
    toward an early-exit (C6/C7). An empty title counts — there is nothing
    to compare, the same rule filter_candidates uses. A title with the
    model code always counts."""
    if not (title or "").strip():
        return True
    if tier(title, product) == 1:
        return True
    return similarity(title, product) >= RELEVANT_MIN_SCORE


def relevant_candidate(candidate, product: set[str]) -> bool:
    """relevant_title for a CandidateResult or its to_dict() form."""
    d = candidate.to_dict() if hasattr(candidate, "to_dict") else candidate
    fields = d.get("structured_fields")
    title = str(fields.get("title") or "") if isinstance(fields, dict) else ""
    return relevant_title(title, product)


def filter_candidates(candidates: list, normalized: dict, top: int) -> tuple[list, list]:
    """Keep the best `top` titled listings per source_domain. Ranking: model
    code in the title first, then brand in the title, then the rest; inside
    a group by the share of the product's words (higher first), then by the
    shorter title. Listings with the model code may go beyond `top` (up to
    MODEL_HIT_CAP_FACTOR x top). Untitled listings are always kept. Returns
    (kept, dropped) — kept keeps the candidates' original order, dropped is
    a list of small dicts (url, source_domain, title, score, tier) for the
    trace. top <= 0 means off: everything is kept."""
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
        scores = {i: similarity(titles[i], product) for i in titled}
        tiers = {i: tier(titles[i], product) for i in titled}
        ranked = sorted(titled, key=lambda i: (tiers[i], -scores[i], len(_tokens(titles[i])), i))
        model_hits = [i for i in ranked if tiers[i] == 1]
        keep.update(ranked[:top])
        keep.update(model_hits[: top * MODEL_HIT_CAP_FACTOR])
        for i in ranked:
            if i in keep:
                continue
            d = candidates[i].to_dict() if hasattr(candidates[i], "to_dict") else candidates[i]
            dropped.append({
                "url": d.get("url"),
                "source_domain": d.get("source_domain"),
                "title": titles[i],
                "score": round(scores[i], 3),
                "tier": tiers[i],
            })

    events.emit("prefilter_finished", kept=len(keep), dropped=len(dropped), top=top)
    return [c for i, c in enumerate(candidates) if i in keep], dropped
