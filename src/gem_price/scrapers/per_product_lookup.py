"""Per-product Flipkart lookup driver.

For a set of GeM products in raw_products, generate a clean Flipkart
search query for EACH product, look up the top 3-5 Flipkart results for
that exact product, and store them linked via searched_for_gem_id.

Compared to the old design (independent generic keyword scrape both sides),
this guarantees the Flipkart results actually correspond to the GeM product
being matched, instead of hoping two unrelated searches overlap.

Usage:
    python -m gem_price.scrapers.per_product_lookup --gem-ids 61,62,63,64,65,66,67,68,70,77
        (--gem-ids optional: all Gem products with searchable slugs if omitted)
    python -m gem_price.scrapers.per_product_lookup --gem-ids 61 --dont-save
"""
import argparse
import csv
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

from gem_price.scrapers import flipkart as fk
from gem_price.scrapers.query_builder import build_queries, raw_slug
from gem_price.matching.llm_attributes import get_product_attributes
from gem_price.core.config import settings
from gem_price.core.logging import setup_logging

logger = setup_logging(__name__)

BASE = Path(__file__).resolve().parent.parent.parent.parent
DB_PATH = BASE / "db" / "gem_project.db"
RAW_DIR = BASE / "data" / "raw"

SHORT_TS = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_gem_products(con: sqlite3.Connection, gem_ids: List[int] = None) -> List[tuple]:
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


def search_candidates(query: str, gem_id: int) -> List[dict]:
    """Run one Flipkart search for a specific GeM product. Returns top 5."""
    rows = fk.scrape_flipkart(query, max_results=5, searched_for_gem_id=gem_id)
    logger.info(f"    query '{query}' -> {len(rows)} results")
    return rows


def try_queries(gem_id: int, queries: List[str], max_tries: int = 3) -> List[dict]:
    """Try primary query; on empty result, retry the remaining queries.

    `queries` is a pre-built ordered list (LLM-derived search_query first,
    followed by the regex-generated query_builder fallbacks, deduped).
    """
    if not queries:
        logger.warning(f"no query generated for product #{gem_id}")
        return []
    logger.info(f"  [{gem_id}] queries={queries}")
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
    logger.info(f"loaded {len(gems)} GeM products to look up\n")

    all_rows: List[dict] = []
    summary = []
    for gid, name, link, price, seller in gems:
        try:
            attrs = get_product_attributes(name, link, con)
            llm_query = attrs.get("search_query")
            queries = []
            if llm_query and llm_query.strip():
                queries.append(llm_query.strip())
            for q in build_queries(name, link):
                if q not in queries:
                    queries.append(q)
            logger.info(f"  [{gid}] attrs from {attrs['source']}: brand={attrs.get('brand')}")
            rows = try_queries(gid, queries)
        except Exception as e:
            logger.error(f"ERROR on {name[:30]}: {type(e).__name__}: {e}")
            rows = []
        if rows is None:
            rows = []
        # cap per product at 5 (spec: top 3-5 per product)
        rows = rows[:5]
        # attach the STABLE gem link (not the autoincrement id, which changes
        # if the DB is rebuilt) so load_data can resolve the right gem id.
        for r in rows:
            r["searched_for_gem_link"] = link
        picks = (r["name"][:60] for r in rows)
        logger.info(f"    picks ({len(rows)}): " + (" | ".join(picks).encode("ascii", "ignore").decode()) + "\n")
        summary.append((gid, name, link, len(rows)))
        all_rows.extend(rows)

    real = [r for r in all_rows if r.get("name") and r.get("price")]
    logger.info(f"total stored-capable rows: {len(real)}")
    con.close()

    if args.dont_save:
        return 0

    # 1) save CSV (traceable origin per product)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"flipkart_perproduct_{SHORT_TS}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["source", "name", "price", "seller", "link", "scraped_at", "searched_for_gem_id", "searched_for_gem_link"])
        w.writeheader()
        w.writerows(real)
    logger.info(f"saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())