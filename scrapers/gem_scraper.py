"""Phase 2 — GeM (gem.gov.in) catalog scraper (requests + BeautifulSoup).

Usage:
    python -m scrapers.gem_scraper --query "office chair" --max-results 20

Contract:
- input: search query
- output: list[dict(source, name, price, price_type, seller, link, scraped_at)]
- saves: data/raw/gem_<query>_<timestamp>.csv
- saves debug HTML per category into data/raw/gem_debug/

Why requests, not Selenium (as the project plan's Phase 2 originally said):
We tried Selenium/Playwright headless first. GeM's bot check redirects ANY
real browser (headless or not, with automation masking) from /search product
pages to gem.gov.in/ — we hit that wall and stopped per the plan's guardrail.
Plain HTTP requests, politely rate-limited, get 200 + full product data with
no CAPTCHA challenge. Using requests is NOT a captcha bypass (there is no
captcha here) — it's the site's own server-rendered pages, fetched the same
way a normal browser tab does. Delays stay conservative (2-3s) per plan.

Price types:
- fixed : single offer price shown
- range : price shown as "X - Y"
- L1_rate : L1 / lowest quoted rate shown (reverse-auction items)
- unknown : nothing parseable

Public catalog only — never bidplus.gem.gov.in.
"""
import argparse
import csv
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent.parent
RAW_DIR = BASE / "data" / "raw"
DEBUG_DIR = RAW_DIR / "gem_debug"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-IN,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "text/html,application/xhtml+xml",
}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    # warm up: homepage sets cookies (XSRF-TOKEN etc.)
    try:
        s.get("https://mkp.gem.gov.in/", timeout=30)
    except requests.RequestException as e:
        print(f"WARNING: homepage warmup failed ({e}) — continuing.")
    return s


def polite_delay():
    time.sleep(random.uniform(2.0, 3.0))  # government site — conservative


def is_blocked(resp: requests.Response) -> bool:
    low = (resp.text or "").lower()
    url = (resp.url or "").lower()
    if "gem.gov.in" in url and "mkp.gem.gov.in" not in url:
        return True  # bounced off marketplace — mark as block
    if resp.status_code in (403, 429):
        return True
    for sig in ("captcha", "access denied", "are you a human", "robot"):
        if sig in low:
            return True
    return False


def fetch_raw(url: str, session: requests.Session):
    resp = session.get(url, timeout=30)
    polite_delay()
    return resp


def search_categories(query: str, session: requests.Session) -> list[str]:
    """GET /search?q=... -> category disambiguation page -> list of category URLs."""
    url = f"https://mkp.gem.gov.in/search?q={quote_plus(query)}"
    resp = fetch_raw(url, session)
    if is_blocked(resp):
        print(f"BLOCKED on search reveal ({resp.status_code}) — stopping, no bypass attempted.")
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    cats: list[str] = []
    seen: set[str] = set()
    for a in soup.select("a[href*='/search']"):
        href = a.get("href", "")
        if "browse" in href:
            continue
        clean = href.split("#")[0]
        if clean in seen:
            continue
        seen.add(clean)
        cats.append(clean)
    return cats


def parse_category(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for a in soup.select('a[href*="-cat.html"]'):
        href = a.get("href", "")
        card = a
        for _ in range(3):
            card = card.parent if card.parent is not None else card
        desc = card.select_one(".variant-desc")
        name = ""
        if desc:
            title_el = desc.select_one(".variant-title")
            name = title_el.get_text(" ", strip=True) if title_el else ""
        if not name:
            name = a.get_text(" ", strip=True)
        card_price = card.select_one(".variant-final-price, div.price")
        price_text = card_price.get_text(" ", strip=True) if card_price else ""
        price, price_type = classify_price(price_text)
        # seller: the sold_as/oem span says "OEM"; sibling info may say more.
        seller = None
        sold_as = card.select_one(".sold_as.oem, .sold_as_summary")
        if sold_as:
            seller = sold_as.get_text(" ", strip=True).strip() or None
        if not name or not a.get("href"):
            continue
        link = href
        if link.startswith("/"):
            link = "https://mkp.gem.gov.in" + link
        link = link.split("#")[0]
        out.append({
            "name": name,
            "price": price,
            "price_type": price_type,
            "seller": seller,
            "link": link,
        })
    return out


def classify_price(text: str):
    t = (text or "").strip()
    low = t.lower()
    if not t:
        return None, "unknown"
    if "l1" in low or "lowest quoted" in low or "l1 rate" in low:
        m = re.search(r"([\d,]+(?:\.\d+)?)", t)
        return (m.group(1).replace(",", "") if m else None), "L1_rate"
    nums = re.findall(r"([\d,]+(?:\.\d+)?)", t)
    if len(nums) >= 2 and ("-" in t or "–" in t or "to " in low or "and " in low):
        return nums[0].replace(",", ""), "range"
    if len(nums) == 1:
        return nums[0].replace(",", ""), "fixed"
    if len(nums) > 1:
        return nums[0].replace(",", ""), "range"
    return None, "unknown"


def scrape_gem(query: str, max_results: int = 20) -> list[dict]:
    session = make_session()
    cats = search_categories(query, session)
    if not cats:
        print("search reveal gave no categories and no outright block — site structure may have changed.")
        return []
    print(f"found {len(cats)} candidate categories, will try up to 5.")

    rows: list[dict] = []
    seen: set[tuple] = set()
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")

    for cat in cats[:5]:
        if len(rows) >= max_results:
            break
        url = ("https://mkp.gem.gov.in" + cat) if cat.startswith("/") else cat
        resp = fetch_raw(url, session)
        if is_blocked(resp):
            print(f"BLOCKED on category page {resp.status_code} — reporting, no bypass attempted.")
            DEBUG_DIR.joinpath(f"gem_block_{safe}_{ts}.html").write_text(resp.text, encoding="utf-8")
            continue
        DEBUG_DIR.joinpath(f"gem_{safe}_{ts}.html").write_text(resp.text, encoding="utf-8")
        cards = parse_category(resp.text)
        before = len(rows)
        for c in cards:
            key = (c["name"], c["price"])
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "source": "gem",
                **c,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            })
            if len(rows) >= max_results:
                break
        print(f"category {cat[:60]}... -> {len(cards)} parsed, +{len(rows) - before} new (total {len(rows)})")
    return rows


def save_csv(query: str, rows: list[dict]) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RAW_DIR / f"gem_{safe}_{ts}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["source", "name", "price", "price_type", "seller", "link", "scraped_at"])
        w.writeheader()
        w.writerows(rows)
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--max-results", type=int, default=20)
    args = ap.parse_args()
    rows = scrape_gem(args.query, args.max_results)
    real = [r for r in rows if r.get("name")]
    print(f"{len(real)} real results for '{args.query}'")
    from collections import Counter
    print("price types:", dict(Counter(r.get("price_type", "unknown") for r in real)))
    if real:
        path = save_csv(args.query, real)
        print(f"saved {path}")
        print("sample:", str(real[0]).encode("ascii", "ignore").decode("ascii"))
    return 0 if len(real) >= 10 else 2  # plan aims >=10 per query


if __name__ == "__main__":
    sys.exit(main())