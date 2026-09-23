"""
Part 3 — Discovery layer.

Only used for domains WITHOUT a dedicated scraper — brand/manufacturer
sites, and anything Part 6's expand_search retry step turns up. Amazon and
Flipkart bypass this entirely via their own direct search (see
scrapers/amazon.py, scrapers/flipkart.py) — that changed while building
Part 4, see the build spec doc, section 7.

Runs a query against plain Google Search (the HTML results page, not the
Shopping tab — postponed per your earlier call), filters results, returns
candidate URLs for scrapers/generic.py to visit.

Honesty check before relying on this in practice: Google is known to
CAPTCHA/block repeated automated queries more aggressively than either
Amazon or Flipkart, especially from datacenter/cloud IPs. This module's
parsing logic is tested against mock HTML (same as every other part so
far), but the live scraping behavior against real Google has NOT been
verified from this build — same network restriction as everywhere else.
If it gets blocked in practice, DuckDuckGo's HTML endpoint
(https://html.duckduckgo.com/html/) is a documented, far more
scraper-tolerant drop-in alternative — only _fetch_search_html() below
would need to change, nothing downstream of it.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

from config import USER_AGENT, REQUEST_TIMEOUT_SECONDS, logger

GOOGLE_RESULTS_PER_QUERY = 20
QUERY_DELAY_RANGE = (1.5, 2.5)  # polite delay between queries in one discover_candidates call

# Google's own domains and common non-retail sites that show up in general
# product searches but are never the actual seller — filtered out whether
# or not an explicit allowed_domains list is given.
_ALWAYS_EXCLUDE = {
    "google.com", "webcache.googleusercontent.com", "translate.google.com",
    "accounts.google.com", "support.google.com", "maps.google.com",
    "policies.google.com", "youtube.com",
}
# Soft blocklist applied only when NO allowed_domains is given (first-pass,
# open brand-site discovery) — these are common non-shopping results that
# show up for almost any product query.
_NON_RETAIL_BLOCKLIST = _ALWAYS_EXCLUDE | {
    "wikipedia.org", "reddit.com", "quora.com", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "pinterest.com", "mkp.gem.gov.in", "gem.gov.in",
}


@dataclass
class DiscoveryResult:
    """One call = one query. Part 6 calls this once per normalized query
    and merges the filtered_urls across calls. raw_urls / filtered_urls
    kept separate (not just deduped-and-merged) because that's the
    "what was searched vs what survived filtering" half of the trace/
    explainability view."""

    query: str
    raw_urls: list[str] = field(default_factory=list)
    filtered_urls: list[str] = field(default_factory=list)
    blocked: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "raw_urls": self.raw_urls,
            "filtered_urls": self.filtered_urls,
            "blocked": self.blocked,
            "error": self.error,
        }


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _domain_matches_set(domain: str, domain_set) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domain_set)


def _is_excluded(domain: str) -> bool:
    return _domain_matches_set(domain, _ALWAYS_EXCLUDE)


def _matches_allowed(domain: str, allowed_domains: list[str]) -> bool:
    return _domain_matches_set(domain, allowed_domains)


def _fetch_search_html(query: str, *, session: Optional[requests.Session] = None) -> str:
    """Isolated on purpose — this is the one function to swap if Google
    needs replacing with DuckDuckGo or another backend."""
    sess = session or requests.Session()
    url = f"https://www.google.com/search?q={quote_plus(query)}&num={GOOGLE_RESULTS_PER_QUERY}&hl=en&gl=in"
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"}
    resp = sess.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.text


_BLOCK_PHRASES = ["detected unusual traffic", "/sorry/index", "our systems have detected"]


def _looks_blocked(html: str) -> bool:
    low = html.lower()
    return any(p in low for p in _BLOCK_PHRASES)


def _parse_result_urls(html: str) -> list[str]:
    """Handles two link shapes Google's results page uses: older-style
    redirect links (/url?q=<real_url>&...) and modern direct <a href>
    elements that wrap an <h3> title. Both are checked since either can
    show up depending on how Google renders for a given request."""
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []

    for a in soup.select('a[href^="/url?q="]'):
        qs = parse_qs(urlparse(a["href"]).query)
        target = qs.get("q", [None])[0]
        if target and target.startswith("http"):
            urls.append(target)

    for h3 in soup.select("h3"):
        a = h3.find_parent("a")
        if a and a.get("href", "").startswith("http"):
            urls.append(a["href"])

    seen, uniq = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def discover_candidates(
    query: str,
    allowed_domains: Optional[list[str]] = None,
    *,
    session: Optional[requests.Session] = None,
) -> DiscoveryResult:
    """allowed_domains=None means open discovery (first pass, brand sites) —
    soft-blocklist common non-retail domains, keep everything else.
    allowed_domains=[...] means strict mode (retry pass, using
    expand_search's suggested_domains) — keep only exact/subdomain matches."""
    try:
        html = _fetch_search_html(query, session=session)
    except Exception as e:
        logger.warning("Google search failed for %r: %s: %s", query, type(e).__name__, e)
        return DiscoveryResult(query=query, error=f"{type(e).__name__}: {e}")

    if _looks_blocked(html):
        logger.warning("Google appears to have blocked/CAPTCHA'd the query %r — stopping, no bypass.", query)
        return DiscoveryResult(query=query, blocked=True)

    raw_urls = _parse_result_urls(html)

    filtered = []
    for u in raw_urls:
        domain = _domain_of(u)
        if not domain or _is_excluded(domain):
            continue
        if allowed_domains:
            if _matches_allowed(domain, allowed_domains):
                filtered.append(u)
        else:
            if not _domain_matches_set(domain, _NON_RETAIL_BLOCKLIST):
                filtered.append(u)

    return DiscoveryResult(query=query, raw_urls=raw_urls, filtered_urls=filtered)


def discover_for_queries(
    queries: list[str],
    allowed_domains: Optional[list[str]] = None,
    *,
    session: Optional[requests.Session] = None,
) -> list[DiscoveryResult]:
    """Runs discover_candidates for each query with a polite delay between
    them. This is what Part 6 actually calls — one DiscoveryResult per
    query, so the trace shows exactly what was searched and what survived
    filtering for each one, not just a flattened final list."""
    results = []
    for i, q in enumerate(queries):
        results.append(discover_candidates(q, allowed_domains, session=session))
        if i < len(queries) - 1:
            time.sleep(random.uniform(*QUERY_DELAY_RANGE))
    return results


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python discovery.py <query> [allowed_domain1,allowed_domain2,...]")
        sys.exit(1)

    domains = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    result = discover_candidates(sys.argv[1], domains)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
