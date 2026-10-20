"""Hide key-shaped text before it reaches a trace, an event, the API or a log.

Providers sometimes put the key they rejected into their error message
("Incorrect API key provided: nvapi-..."). That text used to flow straight
into the trace JSON and the SSE event stream, which are both readable from
the browser. redact() is the single place that knows what a secret looks
like; redact_obj() walks a whole record so callers do not have to know
which field holds the error.
"""
from __future__ import annotations

import re

HIDDEN = "[hidden]"

_PATTERNS = (
    re.compile(r"nvapi-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"gsk_[A-Za-z0-9_\-]{6,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{16,}"),
    re.compile(r"sk-or-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token)\s*[=:]\s*[^\s,;\"']+"),
    re.compile(r"\b[0-9A-Fa-f]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b"),
)


def redact(text) -> str:
    """Return text with anything key-shaped replaced by [hidden]."""
    if not isinstance(text, str):
        text = str(text)
    for pattern in _PATTERNS:
        text = pattern.sub(HIDDEN, text)
    return text


def redact_obj(value):
    """redact() applied to every string inside a dict/list record."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_obj(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_obj(item) for item in value)
    return value
