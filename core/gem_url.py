"""The one GeM-link check the API and the CLI share.

Kept out of api/server.py so the CLI does not have to import FastAPI just to
answer "is this a GeM product link?".
"""
from __future__ import annotations

from urllib.parse import urlparse


def looks_like_gem_url(url: str) -> bool:
    """True for an http(s) link on gem.gov.in or any subdomain of it."""
    parts = urlparse((url or "").strip())
    host = (parts.hostname or "").lower()
    return parts.scheme in ("http", "https") and (
        host == "gem.gov.in" or host.endswith(".gem.gov.in")
    )