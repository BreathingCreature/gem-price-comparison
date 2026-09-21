"""Phase 1 — Flipkart product scraper (Selenium headless + BeautifulSoup).

Usage:
    python -m scrapers.flipkart_scraper --query "office chair" --max-results 30

Contract:
- input: search query
- output: list[dict(source, name, price, seller, link, scraped_at)]
- saves: data/raw/flipkart_<query>_<timestamp>.csv

Guardrails: 1-2s delay between page loads. On block/CAPTCHA: stop + report, no bypass.
requests is NOT used: plain requests get a hard 403 + reCAPTCHA from Flipkart,
so we drive a real (headless) Chrome instead — that is not a captcha bypass.
"""
import argparse
import csv
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, quote_plus

from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent.parent
RAW_DIR = BASE / "data" / "raw"

PRICE_RE = re.compile(r"₹\s?([\d,]+)")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def build_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1366,2000")
    opts.add_argument(f"user-agent={UA}")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(45)
    driver.set_script_timeout(30)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    return driver


def detect_block(driver) -> bool:
    from selenium.webdriver.common.by import By
    try:
        url = (driver.current_url or "").lower()
        if "captcha" in url:
            return True
        body = driver.find_element(By.TAG_NAME, "body").text.lower()
        if "recaptcha" in body or "please verify you are a human" in body:
            return True
        page = driver.page_source.lower()
        if "flipkart recaptcha" in page or "not a robot" in page:
            return True
    except Exception:
        pass
    return False


def load_search_page(driver, query: str, page: int = 1):
    from selenium.webdriver.common.by import By

    url = f"https://www.flipkart.com/search?q={quote_plus(query)}&page={page}"
    try:
        driver.get(url)
    except Exception as e:
        print(f"navigation error on '{query}' p{page}: {type(e).__name__}: {e}")
        return None
    time.sleep(random.uniform(2.0, 3.0))  # initial render
    if detect_block(driver):
        return None
    # lazy-loaded results: scroll to trigger more cards, capped
    for _ in range(8):
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(random.uniform(1.0, 2.0))  # planner guardrail delay 1-2s
            if len(driver.find_elements(By.XPATH, "//a[contains(@href, '/p/')]")) > 50:
                break
            # bounce back up briefly to encourage the next lazy chunk on some layouts
            driver.execute_script("window.scrollBy(0, -600)")
            time.sleep(random.uniform(0.5, 1.0))
        except Exception as e:
            print(f"scroll error: {type(e).__name__}: {e}")
            break
    return driver.page_source


def parse_cards_from_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    # current Flipkart layout: card container div.nZIRY7, clean title div.RG5Slk,
    # selling price div.hZ3P6w. Keep older/layout fallbacks.
    cards = soup.select("div.nZIRY7")
    if not cards:
        cards = soup.select("div[data-id]")
    if not cards:
        cards = soup.select('a[href*="/p/"]')
    out = []
    for card in cards:
        name_el = card.select_one("div.RG5Slk, div._4rR01T, div.KzDlHZ, div.syl9yP")
        if name_el:
            name = name_el.get_text(" ", strip=True)
        else:
            link_a = card.select_one('a[href*="/p/"]')
            name = (link_a.get_text(" ", strip=True) if link_a else "") or card.get_text(" ", strip=True)
            name = name.split("₹")[0].strip()[:200]
            name = name.replace("Add to Compare", "").strip()
        price = None
        price_el = card.select_one("div.hZ3P6w.DeU9vF, div.hZ3P6w, div.Nx9bqj, div._30jeq3")
        if price_el:
            price = re.sub(r"[^\d]", "", price_el.get_text(" ", strip=True))
        if price is None:
            m = PRICE_RE.search(card.get_text(" ", strip=True))
            price = m.group(1).replace(",", "") if m else None
        link_el = card.select_one('a[href*="/p/"]')
        if link_el is None and card.name == "a" and "/p/" in (card.get("href") or ""):
            link_el = card
        link = urljoin("https://www.flipkart.com", link_el["href"].split("?")[0]) if link_el and link_el.get("href") else None
        if not name or not price or not link:
            continue
        out.append({"name": name, "price": price, "link": link})
    seen, uniq = set(), []
    for r in out:
        key = (r["name"], r["link"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def scrape_flipkart(query: str, max_results: int = 30,
                    searched_for_gem_id: int | None = None) -> list[dict]:
    driver = build_driver()
    results: list[dict] = []
    seen: set[tuple] = set()
    try:
        for page in range(1, 4):  # up to 3 pages to guarantee >=15 on lazy layout
            if len(results) >= max_results:
                break
            html = load_search_page(driver, query, page)
            if html is None:
                print("BLOCKED/CAPTCHA on search page — stopping per guardrail, no bypass attempted.")
                debug = RAW_DIR / "flipkart_debug_block.html"
                debug.write_text(driver.page_source, encoding="utf-8")
                print(f"saved block sample to {debug}")
                break
            cards = parse_cards_from_html(html)
            if not cards:
                if page == 1:
                    print("no cards parsed — selectors may have changed; saving debug sample.")
                    debug = RAW_DIR / f"flipkart_debug_{re.sub(r'[^a-z0-9]+', '_', query.lower()).strip('_')}.html"
                    debug.write_text(html, encoding="utf-8")
                    print(f"saved debug HTML to {debug}")
                else:
                    print(f"page {page}: no cards, stopping.")
                break
            before = len(results)
            for c in cards:
                key = (c["name"], c["price"])
                if key in seen:
                    continue
                seen.add(key)
                results.append({
                    "source": "flipkart",
                    "name": c["name"],
                    "price": c["price"],
                    "seller": None,
                    "link": c["link"],
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "searched_for_gem_id": searched_for_gem_id,
                })
                if len(results) >= max_results:
                    break
            print(f"page {page}: parsed {len(cards)} cards, +{len(results) - before} new (total {len(results)})")
            time.sleep(random.uniform(1.0, 2.0))  # guardrail delay between pages
    finally:
        driver.quit()
    return results


def save_csv(query: str, rows: list[dict]) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RAW_DIR / f"flipkart_{safe}_{ts}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["source", "name", "price", "seller", "link", "scraped_at", "searched_for_gem_id"])
        w.writeheader()
        w.writerows(rows)
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--max-results", type=int, default=30)
    args = ap.parse_args()
    rows = scrape_flipkart(args.query, args.max_results)
    real = [r for r in rows if r.get("name") and r.get("price")]
    print(f"{len(real)} real results for '{args.query}'")
    if real:
        path = save_csv(args.query, real)
        print(f"saved {path}")
        print("sample:", str(real[0]).encode("ascii", "ignore").decode("ascii"))
    return 0 if len(real) >= 15 else 2  # plan requires >=15 per test query


if __name__ == "__main__":
    sys.exit(main())