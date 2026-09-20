"""Per-product matching (architecture fix, step 6).

For each GeM product, fuzzy-score its enriched slug_name against ONLY the
3-5 Flipkart candidates that were looked up FOR that product
(raw_products.searched_for_gem_id), using the existing regex hard-filter
+ normalization. Pick the best candidate above threshold 75.

This replaces the old "score against entire Flipkart table" design.

Usage:
    python -m matching.per_product_match --threshold 75
    python -m matching.per_product_match --gem-ids 61,62,63 --dont-write
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

from scrapers.query_builder import build_queries, raw_slug
from matching.match_engine import (normalize, extract_attributes,
                                   extract_slug as slug_words, passes_filter)

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "db" / "gem_project.db"


def load_linked(con, gem_ids=None):
    """Return two structures:
    gem:   dict[id] = (name, link, price)
    fk:    dict[gem_id] -> list[(fkid, name, price)]
    """
    if gem_ids:
        qmarks = ",".join("?" * len(gem_ids))
        gem_rows = con.execute(
            f"SELECT id, name, link, price FROM raw_products "
            f"WHERE source='gem' AND id IN ({qmarks})", gem_ids).fetchall()
        fk_rows = con.execute(
            f"SELECT id, name, price, searched_for_gem_id FROM raw_products "
            f"WHERE source='flipkart' AND searched_for_gem_id IN ({qmarks})",
            gem_ids).fetchall()
    else:
        gem_rows = con.execute(
            "SELECT id, name, link, price FROM raw_products WHERE source='gem' "
            "AND id IN (SELECT DISTINCT searched_for_gem_id FROM raw_products "
            "WHERE searched_for_gem_id IS NOT NULL)").fetchall()
        fk_rows = con.execute(
            "SELECT id, name, price, searched_for_gem_id FROM raw_products "
            "WHERE source='flipkart' AND searched_for_gem_id IS NOT NULL").fetchall()
    gems = {r[0]: (r[1], r[2], r[3]) for r in gem_rows}
    fk = {}
    for fid, name, price, gid in fk_rows:
        fk.setdefault(gid, []).append((fid, name, price))
    return gems, fk


def score_product(gem_name, gem_link, candidates):
    """Score one GeM product against its own candidates. Returns list of
    (score, name, price, fkid) sorted desc."""
    slug = slug_words(gem_link or "")
    enriched = f"{gem_name or ''} {slug}".strip()
    gen_norm = normalize(enriched)
    g_attrs = extract_attributes(enriched)
    out = []
    for fid, mname, mprice in candidates:
        if not passes_filter(g_attrs, mname or ""):
            continue
        s = fuzz.token_sort_ratio(gen_norm, normalize(mname or ""))
        out.append((s, mname, mprice, fid))
    out.sort(key=lambda r: -r[0])
    return out


def _csv_in(x):
    return str(x or "").replace(";", ",")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=75)
    ap.add_argument("--gem-ids", default="")
    ap.add_argument("--dont-write", action="store_true")
    ap.add_argument("--csv", default="", help="write machine-readable report to this path")
    args = ap.parse_args()

    con = sqlite3.connect(DB_PATH)
    gem_ids = [int(x) for x in args.gem_ids.split(",") if x.strip()] if args.gem_ids else None
    gems, linked = load_linked(con, gem_ids)
    if not gems:
        print("no GeM products with linked Flipkart rows — run"
              " `python -m scrapers.per_product_lookup --gem-ids ...` first.")
        return 1

    results = []   # (gid, best_score, best_fkid)
    report_rows = []
    now = datetime.now(timezone.utc).isoformat()

    print(f"{'GEM name/slug':55} {'query':38} {'best cand (score)':50} {'FK Rs':>9} {'match?':>6}")
    print("-" * 170)
    for gid in sorted(gems):
        name, link, _ = gems[gid]
        cands = linked.get(gid, [])
        scored = score_product(name, link, cands)
        queries = build_queries(name, link)
        queries_s = ", ".join(queries[:2]) if queries else "(none)"
        best = scored[0] if scored else None
        matched = best and best[0] >= args.threshold
        if matched:
            results.append((gid, best[3], best[0]))
        best_s = ""
        if best:
            best_s = f"{best[1][:48]:50} ({best[0]:.1f})"
        match_s = "YES" if matched else ("NO" if scored else "no-cand")
        # keep lines ascii-safe for cp1252 console
        print(f"{(name or '')[:40]:42} {'|' if False else ''} {queries_s[:40]:40}"
              f" {best_s:52} {str(best[2] if best else ''):>9} {match_s:>6}")

        slug = raw_slug(link or "")
        for sn, mname, mprice, _ in scored:
            report_rows.append({
                "gem_id": gid,
                "gem_name": name,
                "gem_slug": slug,
                "gem_price": gems[gid][2],
                "query_primary": queries[0] if queries else "",
                "query_fallback": queries[1] if len(queries) > 1 else "",
                "fk_name": mname,
                "fk_price": mprice,
                "similarity": round(sn, 1),
                "matched": "YES" if matched and sn == best[0] else "NO",
            })

    if args.csv:
        import csv as csvlib
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csvlib.DictWriter(f, fieldnames=list(report_rows[0].keys()) if report_rows else
                                  ["gem_id", "gem_name", "gem_slug", "gem_price",
                                   "query_primary", "query_fallback", "fk_name",
                                   "fk_price", "similarity", "matched"])
            w.writeheader()
            w.writerows(report_rows)
        print(f"report written to {args.csv}")

    # write matches
    if not args.dont_write:
        cur = con.cursor()
        cur.execute("DELETE FROM matched_products WHERE match_method='per-product+slug+fuzzy'")
        for gid, fkid, score in results:
            cur.execute(
                "INSERT INTO matched_products (gem_product_id, market_product_id, similarity_score, match_method, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (gid, fkid, float(score), "per-product+slug+fuzzy", now))
        con.commit()
    con.close()

    print(f"\nproducts scored: {len(gems)} | matches above {args.threshold:g}: {len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())