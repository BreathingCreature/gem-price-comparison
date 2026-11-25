"""Helpers: which shops to retry, and what to search for.

The retry itself (a search plus verify per shop) belongs to the pipeline; the
decisions live here so they can be tested without one.

A retry spends a search and an LLM expansion. It is worth spending only when
the failure can change: a timeout, a connection reset, a server error, an
unfinished lane. A 403/404/410, a CAPTCHA or bot wall, a robots disallow, a
DNS failure and a document-page rejection will look exactly the same with new
search words, so they never get a second try.
"""
from __future__ import annotations

import re

from core.result import (
    SHOP_FOUND,
    SHOP_INCOMPLETE,
    SHOP_NOT_FOUND,
    SHOP_NOT_SEARCHED,
    SHOP_POSSIBLE,
    SHOP_TIMED_OUT,
)

DIRECT_SHOPS = ("flipkart.com", "amazon.in")
UNFINISHED = (SHOP_INCOMPLETE, SHOP_TIMED_OUT)

_HARD_REASON = re.compile(
    r"\b403\b|\b404\b|\b410\b|captcha|bot wall|robot check|blocked|robots(\.txt| disallow)?"
    r"|NameResolutionError|\bDNS\b|document, not a product page|not a public http",
    re.IGNORECASE,
)
_CHANGEABLE_REASON = re.compile(
    r"timeout|timed out|took \d+ s \(limit|connection(error|reset)|reset by peer"
    r"|\b5\d\d\b|server error|could not be (checked|searched)|web search failed"
    r"|no result links could be parsed",
    re.IGNORECASE,
)
_NEUTRAL_REASONS = {"", "searched, nothing confirmed", "the search returned no listings"}


def transient_reason(reason) -> bool:
    """True for a failure a second attempt could plausibly fix. Used both to
    name the card `incomplete` and to decide whether to spend a retry."""
    text = str(reason or "")
    return not _HARD_REASON.search(text) and bool(_CHANGEABLE_REASON.search(text))


def is_retryable(name: str, entry: dict, *, nothing_found: bool, confirmed: bool = False) -> bool:
    """One shop's retry decision, shared by the card and the retry planner.

    `confirmed` is the run's first-pass answer. Once a match exists, only a
    timed-out or incomplete Flipkart/Amazon lane can still add anything: new
    words cannot unblock a web shop's 403, and the confirmed match is already
    on the card.
    """
    entry = entry or {}
    if entry.get("retried") or not entry.get("retryable", True):
        return False
    status = entry.get("status")
    if status in (SHOP_FOUND, SHOP_POSSIBLE):
        return False
    reason = str(entry.get("reason") or "")
    if _HARD_REASON.search(reason):
        return False
    unfinished = status in UNFINISHED
    if confirmed:
        return name in DIRECT_SHOPS and unfinished and bool(_CHANGEABLE_REASON.search(reason))
    if unfinished:
        return bool(_CHANGEABLE_REASON.search(reason))
    if status == SHOP_NOT_FOUND and nothing_found:
        return True
    if status == SHOP_NOT_SEARCHED and nothing_found:
        return reason in _NEUTRAL_REASONS or bool(_CHANGEABLE_REASON.search(reason))
    return False


def retryable_shops(shops: dict, *, nothing_found: bool, confirmed: bool = False) -> list[str]:
    """Shop names worth one retry, in card order."""
    return [
        name
        for name, entry in (shops or {}).items()
        if is_retryable(name, entry, nothing_found=nothing_found, confirmed=confirmed)
    ]


def fresh_queries(expansion: dict, tried_queries: list[str]) -> list[str]:
    """The new words worth trying: stripped, de-duplicated, not already used.
    An LLM that repeats the first-pass queries contributes nothing."""
    fresh: list[str] = []
    for query in (expansion or {}).get("new_queries") or []:
        text = str(query).strip()
        if text and text not in tried_queries and text not in fresh:
            fresh.append(text)
    return fresh
