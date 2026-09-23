"""
Part 3 — Discovery layer.

Only used for domains WITHOUT a dedicated scraper — brand/manufacturer
sites, and anything Part 6's expand_search retry step turns up. Amazon and
Flipkart bypass this entirely via their own direct search (see
scrapers/amazon.py, scrapers/flipkart.py).

Backend: DuckDuckGo's HTML endpoint (https://html.duckduckgo.com/html/)
is primary — Google's results page went JS-only (live-verified 2026-09:
HTTP 200, zero <h3>, zero /url?q= links, so it returned nothing while
reporting blocked=False). Google remains a fallback fetch in case DDG is
down/blocked; _parse_result_urls() understands BOTH link shapes (DDG's
//duckduckgo.com/l/?uddg= redirects and Google's /url?q= + h3 styles), so
nothing downstream of fetching changed.

Honesty: a backend that responds but yields zero parseable links now
returns DiscoveryResult.error set ("backends responded but no result
links...") instead of a silent empty success — silent empties made
scrape failures indistinguishable from genuinely absent results.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

from config import USER_AGENT, REQUEST_TIMEOUT_SECONDS, logger

GOOGLE_RESULTS_PER_QUERY = 20
QUERY_DELAY_RANGE = (1.5, 2.5)  # polite delay between queries in one discover_candidates call

# Search engines' own domains and common non-retail sites that show up in
# general product searches but are never the actual seller — filtered out
# whether or not an explicit allowed_domains list is given.
_ALWAYS_EXCLUDE = {
    "google.com", "webcache.googleusercontent.com", "translate.google.com",
    "accounts.google.com", "support.google.com", "maps.google.com",
    "policies.google.com", "youtube.com",
    "duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com",
}
# Soft blocklist applied only when NO allowed_domains is given (first-pass,
# open brand-site discovery) — these are common non-shopping results that
# show up for almost any product query.
_NON_RETAIL_BLOCKLIST = _ALWAYS_EXCLUDE | {
    "wikipedia.org", "reddit.com", "quora.com", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "pinterest.com", "mkp.gem.gov.in", "gem.gov.in",
    # Non-Indian stores / non-product pages that crowd real candidates out of
    # pipeline's 8-URL generic-scrape cap (seen live in DDG results).
    "amazon.com", "walmart.com", "bhphotovideo.com", "tomsguide.com",
    "manuals.plus", "ebay.com",
}
# Domains with their own direct scrapers — discovery must not surface them
# for generic re-scraping (that caused duplicate candidates + wasted 403
# fetches against Flipkart/Amazon when discovery worked again).
_DIRECT_SCRAPER_DOMAINS = {"amazon.in", "flipkart.com"}

# Open discovery returns INDIAN retail sites only (user directive
# 2026-09-24: "visit amazon.in not us, indian sites not the other ones").
# Rule: any .in / .co.in domain, or one of these known Indian marketplaces
# that use .com.
_INDIAN_RETAIL_DOMAINS = {
    "flipkart.com", "amazon.in", "snapdeal.com", "paytmmall.com",
    "indiamart.com", "industrybuying.com", "moglix.com", "croma.com",
    "tatacliq.com", "vijaysales.com", "jiomart.com", "bigbasket.com",
    "sangeetha.com", "poorvika.com", "reliancedigital.in", "ubuy.co.in",
    "pepperfry.com", "shopclues.com", "firstcry.com",
}


def _is_indian_retail(domain: str) -> bool:
    if domain.endswith(".in") or domain.endswith(".co.in"):
        return True
    return _domain_matches_set(domain, _INDIAN_RETAIL_DOMAINS)


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


def _fetch_ddg_html(query: str, *, session: Optional[requests.Session] = None) -> str:
    """Primary backend — DuckDuckGo's plain-HTML endpoint, far more
    scraper-tolerant than Google. Isolated like _fetch_google_html so either
    can be swapped without touching anything downstream."""
    sess = session or requests.Session()
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"}
    resp = sess.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.text


def _fetch_google_html(query: str, *, session: Optional[requests.Session] = None) -> str:
    """Fallback backend. Live-tested as JS-only/empty from this machine
    (see module docstring) — kept in case Google serves real HTML again or
    DDG is the one that's down."""
    sess = session or requests.Session()
    url = f"https://www.google.com/search?q={quote_plus(query)}&num={GOOGLE_RESULTS_PER_QUERY}&hl=en&gl=in"
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"}
    resp = sess.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.text


# Backends tried per query, in order.
_FETCHERS = (_fetch_ddg_html, _fetch_google_html)

_BLOCK_PHRASES = [
    "detected unusual traffic", "/sorry/index", "our systems have detected",
    "bots use duckduckgo", "automated queries", "too many requests",
]


def _looks_blocked(html: str) -> bool:
    low = html.lower()
    return any(p in low for p in _BLOCK_PHRASES)


def _parse_result_urls(html: str) -> list[str]:
    """Handles the link shapes search engines actually use:
    - DuckDuckGo HTML: //duckduckgo.com/l/?uddg=<urlencoded real url>&...
    - Google old-style: /url?q=<real_url>&...
    - Google modern: direct <a href> wrapping an <h3> title.
    - DDG direct result anchors (a.result__a) when not redirect-wrapped.
    """
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []

    # DDG redirect links (also handles lite endpoint plain hrefs to uddg=)
    for a in soup.select('a[href*="uddg="]'):
        href = a["href"]
        if href.startswith("//"):
            href = "https:" + href
        qs = parse_qs(urlparse(href).query)
        target = qs.get("uddg", [None])[0]
        if target and target.startswith("http"):
            urls.append(target)

    # Google old-style redirect links
    for a in soup.select('a[href^="/url?q="]'):
        qs = parse_qs(urlparse(a["href"]).query)
        target = qs.get("q", [None])[0]
        if target and target.startswith("http"):
            urls.append(target)

    # Modern direct links wrapping <h3>
    for h3 in soup.select("h3"):
        a = h3.find_parent("a")
        if a and a.get("href", "").startswith("http"):
            urls.append(a["href"])

    # DDG result anchors with plain (non-redirect) hrefs
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        if href.startswith("http") and "duckduckgo.com" not in href:
            urls.append(href)

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
    expand_search's suggested_domains) — keep only exact/subdomain matches.

    Tries each backend in _FETCHERS until one yields parseable links.
    """
    raw_urls: list[str] = []
    got_response = False
    blocked_any = False
    last_err: Optional[str] = None

    for fetch in _FETCHERS:
        try:
            html = fetch(query, session=session)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.warning("%s failed for %r: %s", fetch.__name__, query, last_err)
            continue
        got_response = True
        if _looks_blocked(html):
            logger.warning("%s blocked/CAPTCHA'd the query %r — trying next backend.", fetch.__name__, query)
            blocked_any = True
            continue
        raw_urls = _parse_result_urls(html)
        if raw_urls:
            break

    if not got_response:
        return DiscoveryResult(query=query, error=last_err or "all search backends failed")
    if not raw_urls:
        if blocked_any:
            return DiscoveryResult(query=query, blocked=True)
        # Responded fine but yielded nothing parseable — NOT a silent empty
        # success; downstream must be able to tell "engine broken" from
        # "nothing on the internet matches".
        return DiscoveryResult(
            query=query,
            error="search backends responded but no result links could be parsed",
        )

    filtered = []
    for u in raw_urls:
        domain = _domain_of(u)
        if not domain or _is_excluded(domain):
            continue
        if allowed_domains:
            if _matches_allowed(domain, allowed_domains):
                filtered.append(u)
        else:
            if _domain_matches_set(domain, _NON_RETAIL_BLOCKLIST):
                continue
            if _domain_matches_set(domain, _DIRECT_SCRAPER_DOMAINS):
                continue  # Amazon/Flipkart have their own scrapers — don't duplicate
            if not _is_indian_retail(domain):
                continue  # Indian sites only — no .com foreign stores in open discovery
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
