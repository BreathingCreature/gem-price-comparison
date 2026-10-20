"""Small candidate helpers shared by the search, lane and result code.

These are the functions every stage used to borrow from core/pipeline.py;
they are pure (no config, no events) so moving them cannot change a run.
"""
from __future__ import annotations

from core.keys import listing_key


def cand_dict(candidate) -> dict:
    """A candidate as a plain dict, whether or not it is a dataclass."""
    return candidate.to_dict() if hasattr(candidate, "to_dict") else candidate


def usable(candidate) -> bool:
    """False for candidates whose fetch failed with no evidence at all -
    sending those to the LLM lets it 'judge' from the reference alone."""
    d = cand_dict(candidate)
    if not isinstance(d, dict):
        return False
    if d.get("fetch_error") and not d.get("raw_page_text") and not d.get("structured_fields"):
        return False
    return True


def listing_identity(url: str) -> str:
    """The per-listing identity used for dedup and verdict reuse."""
    return listing_key(url)


def dedup(candidates: list) -> list:
    """Cross-source URL dedup (the same Flipkart listing could arrive from
    the direct scraper AND from discovery) - duplicate verify calls and
    duplicate rows in the comparison otherwise."""
    seen: set[str] = set()
    out = []
    for candidate in candidates:
        d = cand_dict(candidate)
        key = listing_identity(d.get("url")) if isinstance(d, dict) else ""
        if not key:
            key = f"__noid_{len(out)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out
