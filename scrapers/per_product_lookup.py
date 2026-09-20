"""Per-product Flipkart lookup driver (architecture fix).

For a set of GeM products in raw_products, generate a clean Flipkart
search query for EACH product, look up the top 3-5 Flipkart results for
that exact product, and store them linked via searched_for_gem_id.

Compared to the old design (independent generic keyword scrape both sides),
this guarantees the Flipkart results actually correspond to the GeM product
being matched, instead of hoping two unrelated searches overlap.

Usage:
    python -m scrapers.per_product_lookup  --gem-ids 61,62,63,64,65,66,67,68,70,77
        (--gem-ids optional: all Gem products with searchable slugs if omitted)
    python -m scrapers.per_product_lookup --gem-ids 61 --dont-save
"""
import argparse
import csv
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from scrapers import flipkart_scraper as fk
from scrapers.query_builder import build_queries

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "db" / "gem_project.db"
RAW_DIR = BASE / "data" / "raw"

SHORT_TS = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_gem_products(con, gem_ids=None):
    if gem_ids:
        qmarks = ",".join("?" * len(gem_ids))
        rows = con.execute(
            f"SELECT id, name, link, price, seller FROM raw_products "
            f"WHERE source='gem' AND id IN ({qmarks}) ORDER BY id",
            gem_ids,
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, name, link, price, seller FROM raw_products "
            "WHERE source='gem' ORDER BY id"
        ).fetchall()
    return rows


def search_candidates(query: str, gem_id: int) -> list[dict]:
    """Run one Flipkart search for a specific GeM product. Returns top 5."""
    rows = fk.scrape_flipkart(query, max_results=5, searched_for_gem_id=gem_id)
    print(f"    query '{query}' -> {len(rows)} results")
    return rows


def try_queries(gem_id: int, name: str, link: str, max_tries: int = 3) -> list[dict]:
    """Try primary query; on empty result, retry fallback queries."""
    queries = build_queries(name, link)
    if not queries:
        print(f"    no query generated (slug missing) for {name[:40]}")
        return []
    print(f"  [{gem_id}] {name[:48]:50} queries={queries}")
    last = []
    for q in queries[:max_tries]:
        rows = search_candidates(q, gem_id)
        if rows:
            return rows
        last = rows
        time.sleep(random.uniform(1.5, 2.5))
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gem-ids", default="")
    ap.add_argument("--dont-save", action="store_true", help="print only, no CSV/DB write")
    args = ap.parse_args()

    con = sqlite3.connect(DB_PATH)
    gem_ids = [int(x) for x in args.gem_ids.split(",") if x.strip()] if args.gem_ids else None
    gems = load_gem_products(con, gem_ids)
    print(f"loaded {len(gems)} GeM products to look up\n")
    con.close()

    all_rows: list[dict] = []
    summary = []
    for gid, name, link, price, seller in gems:
        rows = try_queries(gid, name, link)
        # cap per product at 5 (spec: top 3-5 per product)
        rows = rows[:5]
        # attach the STABLE gem link (not the autoincrement id, which changes
        # if the DB is rebuilt) so load_data can resolve the right gem id.
        for r in rows:
            r["searched_for_gem_link"] = link
        picks = (r["name"][:60] for r in rows)
        print(f"    picks ({len(rows)}): " + (" | ".join(picks).encode("ascii", "ignore").decode() ) + "\n")
        summary.append((gid, name, link, len(rows)))
        all_rows.extend(rows)

    real = [r for r in all_rows if r.get("name") and r.get("price")]
    print(f"total stored-capable rows: {len(real)}")

    if args.dont_save:
        return 0

    # 1) save CSV (traceable origin per product)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"flipkart_perproduct_{SHORT_TS}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["source", "name", "price", "seller", "link", "scraped_at", "searched_for_gem_id", "searched_for_gem_link"])
        w.writeheader()
        w.writerows(real)
    print(f"saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())