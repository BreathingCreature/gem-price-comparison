"""ADR-001 proof: strip 'fingers' from fallback BRAND_HINTS, warm/read the
attribute cache, and show the LLM path still detects the brand.

USAGE: python -X utf8 diag_llm_proof.py [-n SMOKE_COUNT] [-all]
Run against the live DB. Requires NVIDIA_API_KEY in the environment.
"""
import argparse
import os
import sqlite3
import sys

DB = "db/gem_project.db"
STRIP_BRANDS = ["fingers", "prodot", "lapcare", "hp"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=0, help="limited product count (smoke test)")
    ap.add_argument("--all", action="store_true", help="iterate all articles, not a slice")
    args = ap.parse_args()

    from scrapers import query_builder
    removed = sorted(b for b in STRIP_BRANDS if b in query_builder.BRAND_HINTS)
    for b in STRIP_BRANDS:
        query_builder.BRAND_HINTS.discard(b)
    print(f"stripped {removed} from fallback BRAND_HINTS; remaining={len(query_builder.BRAND_HINTS)}")

    from matching.llm_attributes import get_product_attributes

    con = sqlite3.connect(DB)
    rows = con.execute("SELECT id, name, link, price FROM raw_products WHERE source='gem' ORDER BY id").fetchall()
    if not args.all:
        rows = rows[: args.n or 5]
    print(f"processing {len(rows)} gem products\n")

    stats = {"llm": 0, "fallback_regex": 0}
    brands_llm = set()
    for gid, name, link, price in rows:
        a = get_product_attributes(name, link, con)
        src = a["source"]
        stats[src] = stats.get(src, 0) + 1
        if a.get("brand"):
            brands_llm.add(a["brand"].lower())
        q = (a.get("search_query") or "")[:48]
        print(f"[{gid:>3}] {src:<15} brand={str(a.get('brand')) or '-':<14} model={str(a.get('model')) or '-':<14} query={q}")
    print(f"\nstats: {stats}")
    print(f"brands detected: {sorted(brands_llm)}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())