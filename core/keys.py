"""One listing, one key.

The pipeline dedups candidates by URL and the eval scorer matches predicted
URLs against the answer key. Both used to keep their own rule and quietly
disagreed about query strings: a listing could be "the same listing" during
a run and two different listings during scoring. Everything now goes through
listing_key(), so the two can never drift apart.

The rules:
  - host lowercased, a leading "www." removed, fragment ignored
  - path kept, trailing "/" removed
  - tracking parameters dropped (utm_*, ref, tag, gclid, fbclid, ...)
  - real variant parameters kept (pid, lid, variant, color, size, th, ...)
  - remaining parameters sorted by name
  - Amazon: only the 10-character ASIN matters -> "amazon.in/dp/<asin>"
  - Flipkart: the pid (and lid when present) identifies the listing;
    the title slug in the path does not
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlparse

TRACKING_EXACT = {
    "affid",
    "cmpid",
    "crid",
    "cv_ct_id",
    "dclid",
    "fbclid",
    "gclid",
    "keywords",
    "msclkid",
    "psc",
    "qid",
    "ref",
    "ref_",
    "spm",
    "sprefix",
    "sr",
    "srsltid",
    "tag",
    "utm",
    "yclid",
}

TRACKING_PREFIXES = ("utm_", "pf_rd_", "pd_rd_", "cv_ct_")

_ASIN_RE = r"/(?:dp|gp/product|product)/([A-Za-z0-9]{10})"
_FLIPKART_ITM_RE = r"/p/(itm[a-z0-9]+)"


def _host_and_path(url: str):
    parsed = urlparse(url if "//" in url else "//" + url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").rstrip("/")
    return host, path, parsed


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_EXACT or any(lowered.startswith(p) for p in TRACKING_PREFIXES)


def _kept_params(query: str) -> list[tuple[str, str]]:
    pairs = []
    for name, value in parse_qsl(query, keep_blank_values=True):
        if not name or _is_tracking(name):
            continue
        pairs.append((name.lower(), value))
    return sorted(pairs)


def _query_string(pairs: list[tuple[str, str]]) -> str:
    return "&".join(f"{name}={value}" for name, value in pairs)


def _flipkart_key(host: str, path: str, parsed) -> str:
    params = dict(_kept_params(parsed.query))
    pid = params.get("pid")
    if pid:
        pairs = [("pid", pid)]
        if params.get("lid"):
            pairs.append(("lid", params["lid"]))
        return f"{host}?{_query_string(pairs)}"
    match = re.search(_FLIPKART_ITM_RE, path, re.I)
    if match:
        return f"{host}/p/{match.group(1).lower()}"
    return ""


def host_matches(host: str, domain: str) -> bool:
    """True for the domain itself and its real subdomains, never for a
    lookalike ("amazon.in" matches "www.amazon.in", not "fakeamazon.in")."""
    host, domain = (host or "").lower(), (domain or "").lower()
    return bool(host) and (host == domain or host.endswith("." + domain))


def listing_key(url: str) -> str:
    """The identity of one listing URL. Empty string when there is no URL.

    Two URLs with the same key are the same listing for dedup and scoring.
    """
    url = (url or "").strip()
    if not url:
        return ""
    host, path, parsed = _host_and_path(url)
    if not host:
        return ""

    if host_matches(host, "amazon.in") or host_matches(host, "amazon.com"):
        match = re.search(_ASIN_RE, path)
        if match:
            return f"{host}/dp/{match.group(1).lower()}"

    if host_matches(host, "flipkart.com"):
        key = _flipkart_key(host, path, parsed)
        if key:
            return key

    query = _query_string(_kept_params(parsed.query))
    base = f"{host}{path}"
    return f"{base}?{query}" if query else base
