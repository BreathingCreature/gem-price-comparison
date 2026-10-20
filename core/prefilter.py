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
from typing import Optional

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

# A product with no brand and no model code has fewer words to match, so the
# same real listing scores lower. The bar drops for it instead of punishing
# the listing for data the GeM page never had (Stage 6).
NO_CLUE_RELEVANT_MIN_SCORE = 0.4

# C6: enough relevant cards on a page mean the next Flipkart page cannot add
# much — stop paging there (10-30 s saved on easy products).
ENOUGH_RELEVANT = 5

# Hard cap per shop before the LLM (Stage 6). Tier-1 listings (the model code
# is in the title) are kept beyond the normal cap, up to their own cap; when
# there are more than that, the 20 nearest the GeM price are kept. More than
# 10 ordinary listings per shop was only ever worth LLM calls by accident.
MAX_NON_MODEL_PER_SHOP = 10
MAX_MODEL_HITS_PER_SHOP = 20

# Safety net: if a shop would send NOTHING after the caps, send its best few
# by score anyway. A filtered-out true match is worse than a few wasted
# calls, and a shop with zero listings must never look like "not found".
SAFETY_NET_KEEP = 3

# Kept for older callers/tests that used the old tier-1 multiplier name.
MODEL_HIT_CAP_FACTOR = MAX_MODEL_HITS_PER_SHOP


def has_identity_clues(product) -> bool:
    """True when the product has a brand or a model code to match on."""
    return bool(getattr(product, "brand_words", None)) or bool(getattr(product, "model_keys", None))


def min_relevant_score(product) -> float:
    """The relevance bar for this product. Lower when there is no brand and no
    model to match on (Stage 6.1e): the product has fewer words to share with a
    real title, so the same real listing scores lower."""
    if has_identity_clues(product):
        return RELEVANT_MIN_SCORE
    return NO_CLUE_RELEVANT_MIN_SCORE


def relevant_title(title: str, product) -> bool:
    """True when a listing title is close enough to the product to count
    toward an early-exit (C6/C7). An empty title counts — there is nothing
    to compare, the same rule filter_candidates uses. A title with the
    model code always counts."""
    if not (title or "").strip():
        return True
    if tier(title, product) == 1:
        return True
    return similarity(title, product) >= min_relevant_score(product)


def relevant_candidate(candidate, product: set[str]) -> bool:
    """relevant_title for a CandidateResult or its to_dict() form."""
    d = candidate.to_dict() if hasattr(candidate, "to_dict") else candidate
    fields = d.get("structured_fields")
    title = str(fields.get("title") or "") if isinstance(fields, dict) else ""
    return relevant_title(title, product)


def _candidate_price(candidate) -> Optional[object]:
    """The structured price as it arrived (number or string), or None."""
    d = candidate.to_dict() if hasattr(candidate, "to_dict") else candidate
    fields = d.get("structured_fields")
    if not isinstance(fields, dict):
        return None
    return fields.get("price")


def _price_number(value) -> Optional[float]:
    """A number from the card price, for the tier-1 cap only. Anything that
    cannot be read is None (never guess)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^0-9.]", "", value)
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    return None


def _gem_price(normalized: dict) -> Optional[float]:
    value = normalized.get("gem_price") if isinstance(normalized, dict) else None
    return _price_number(value)


def _price_distance(price, gem_price: Optional[float]) -> float:
    """How far a listing's price is from the GeM price, relative. Unknown on
    either side sorts last (infinity)."""
    if price is None or gem_price is None or gem_price <= 0:
        return float("inf")
    return abs(price - gem_price) / gem_price


def filter_candidates(candidates: list, normalized: dict, top: int) -> tuple[list, list]:
    """Keep the listings worth an LLM call, per source_domain (Stage 6).

    The normal cap is `top` per shop (default 10), never more than
    MAX_NON_MODEL_PER_SHOP. Listings whose title carries the model code
    (tier 1) are always kept, beyond that cap, up to MAX_MODEL_HITS_PER_SHOP;
    when there are more than that, the 20 whose price sits closest to the
    GeM price win. If a shop would keep nothing after the caps, its best
    SAFETY_NET_KEEP listings go through anyway.

    Untitled listings are always kept — there is nothing to compare.
    Returns (kept, dropped): kept keeps the original order; dropped entries
    carry {title, url, price, score, tier, rule} for the trace. top <= 0
    means off: everything is kept."""
    if top <= 0 or not candidates:
        return list(candidates), []

    product = product_tokens(normalized)
    gem_price = _gem_price(normalized)
    titles: list[str] = []
    by_shop: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidates):
        d = candidate.to_dict() if hasattr(candidate, "to_dict") else candidate
        fields = d.get("structured_fields")
        title = str(fields.get("title") or "").strip() if isinstance(fields, dict) else ""
        titles.append(title)
        by_shop.setdefault(str(d.get("source_domain") or "?"), []).append(index)

    # A negative GEM_PREFILTER_TOP cannot happen (config clamps it), but the
    # arithmetic is written so the cap can never exceed the hard limit.
    non_model_limit = min(max(top, 0), MAX_NON_MODEL_PER_SHOP)
    keep: set[int] = set()
    dropped: list[dict] = []
    for indexes in by_shop.values():
        titled = [i for i in indexes if titles[i]]
        keep.update(i for i in indexes if not titles[i])  # no title -> nothing to compare, always kept
        if not titled:
            continue
        scores = {i: similarity(titles[i], product) for i in titled}
        tiers = {i: tier(titles[i], product) for i in titled}
        prices = {i: _price_number(_candidate_price(candidates[i])) for i in titled}
        min_score = min_relevant_score(normalized)
        ranked = sorted(titled, key=lambda i: (tiers[i], -scores[i], len(_tokens(titles[i])), i))
        model_hits = [i for i in ranked if tiers[i] == 1]
        others = [i for i in ranked if tiers[i] != 1 and scores[i] >= min_score]
        keep.update(others[:non_model_limit])
        if len(model_hits) > MAX_MODEL_HITS_PER_SHOP:
            # More model-code hits than the cap: keep the ones priced nearest
            # the GeM product. Unknown prices sort last, not first.
            model_hits = sorted(model_hits, key=lambda i: (_price_distance(prices[i], gem_price), -scores[i], i))
        keep.update(model_hits[:MAX_MODEL_HITS_PER_SHOP])
        if not any(i in keep for i in titled):
            # SAFETY NET (Stage 6.1c): the caps just emptied this shop.
            keep.update(sorted(titled, key=lambda i: (-scores[i], tiers[i], len(_tokens(titles[i])), i))[:SAFETY_NET_KEEP])
        for i in ranked:
            if i in keep:
                continue
            d = candidates[i].to_dict() if hasattr(candidates[i], "to_dict") else candidates[i]
            if tiers[i] == 1:
                rule = "over_model_cap"
            elif scores[i] < min_score:
                rule = "not_relevant"
            else:
                rule = "over_shop_cap"
            dropped.append({
                "url": d.get("url"),
                "source_domain": d.get("source_domain"),
                "title": titles[i],
                "price": _candidate_price(candidates[i]),
                "score": round(scores[i], 3),
                "tier": tiers[i],
                "rule": rule,
            })

    events.emit("prefilter_finished", kept=len(keep), dropped=len(dropped), top=top)
    return [c for i, c in enumerate(candidates) if i in keep], dropped
