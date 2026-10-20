"""Stage 6.5 in its own file: the one 'similar products' question.

Only asked when a run found neither a match nor a possible one. The answer is
a list of different products of the same kind, so it can never be used for
the cheapest price or the savings line.
"""
from __future__ import annotations

from core import prefilter
from llm import client as llm_client

MAX_SIMILAR_INPUTS = 5


def candidates_for_similar(normalized, candidates: list) -> list:
    """Tier 2/3 listings that share at least one product word, best first."""
    product = prefilter.product_tokens(normalized)
    scored = []
    for candidate in candidates:
        item = candidate.to_dict() if hasattr(candidate, "to_dict") else candidate
        fields = item.get("structured_fields")
        title = str(fields.get("title") or "") if isinstance(fields, dict) else ""
        if not title:
            continue
        # A model-code hit is a same-product question, already answered.
        if prefilter.tier(title, product) == 1:
            continue
        score = prefilter.similarity(title, product)
        if score <= 0:
            continue
        scored.append((score, item))
    scored.sort(key=lambda pair: -pair[0])
    return [item for _, item in scored[:MAX_SIMILAR_INPUTS]]


def find_similar(normalized, candidates: list, warnings: list | None = None) -> list:
    """One LLM call over at most five listings. Any failure is a warning."""
    picked = candidates_for_similar(normalized, candidates)
    if not picked:
        return []
    try:
        return llm_client.find_similar(normalized, picked)
    except Exception as exc:  # noqa: BLE001 - a nicety must never break a run
        if warnings is not None:
            warnings.append(f"Similar products check failed: {type(exc).__name__}: {exc}")
        return []
