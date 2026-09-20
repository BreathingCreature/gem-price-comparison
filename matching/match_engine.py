"""Phase 4 — Matching engine v1: regex hard-filter + rapidfuzz fuzzy.

- Loads raw_products split by source ('gem' vs 'flipkart')
- Enriches GeM names with the product URL slug (contains the full model
  name, e.g. "hp-280-g9-sff-i3-12100-win11p-32113"), which search-card
  titles truncate.
- Normalizes names: lowercase, punctuation space, stopword removal so
  short GeM titles are comparable to verbose Flipkart titles.
- Regex attribute extraction: brand, number+unit, model codes
- Hard filter: skip pairs with no shared attributes
- Fuzzy: rapidfuzz token_sort_ratio on normalized names, threshold 75
- Writes matched_products with match_method='regex_filter+slug+fuzzy'
"""
import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "db" / "gem_project.db"

KNOWN_BRANDS = {
    "hp", "dell", "lenovo", "acer", "asus", "apple", "samsung", "lg",
    "canon", "epson", "brother", "godrej", "nilkamal", "featherlite",
    "zebronics", "logitech", "sony", "philips", "havells", "bajaj",
}

UNIT_RE = re.compile(r"(\d+(?:\.\d+)?\s?(?:gb|tb|mb|mm|cm|inch|inches|\"|kg|g|w|v|ah|mah|hz|ghz|mp|ltr|litre|liter))", re.I)
MODEL_RE = re.compile(r"\b([A-Z]{1,4}[-/]?\d{2,}[A-Z0-9-]*)\b")
BRAND_RE = re.compile(r"^([A-Z][a-zA-Z]+)")

STOPWORDS = {
    "with", "and", "the", "of", "for", "in", "on", "at", "by", "to",
    "a", "an", "it", "is", "are", "was", "be", "or", "not", "from",
    "series", "new", "gen", "model", "brand", "feature", "features",
}


def normalize(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    tokens = [w for w in t.split() if w not in STOPWORDS]
    return " ".join(tokens)


def extract_slug(link: str) -> str:
    """GeM link pattern: .../<slug>/p-<id>-<vid>-cat.html  -> slug words."""
    m = re.search(r"/([^/]+)/p-[\d-]+-cat\.html", link or "")
    if not m:
        return ""
    return re.sub(r"[^a-z0-9 ]+", " ", m.group(1).replace("-", " "))


def extract_attributes(name: str) -> dict:
    low = name.lower()
    brand = None
    for b in KNOWN_BRANDS:
        if b in low:
            brand = b
            break
    if not brand:
        m = BRAND_RE.search(name.strip())
        if m:
            brand = m.group(1).lower()
    units = {u.lower().replace(" ", "") for u in UNIT_RE.findall(name)}
    models = {m.upper() for m in MODEL_RE.findall(name)}
    return {"brand": brand, "units": units, "models": models}


def passes_filter(gem_attrs: dict, flip_name: str) -> bool:
    """Hard filter: at least one shared signal, else skip fuzzy scoring."""
    flip_low = flip_name.lower()
    # brand must match if we extracted one
    if gem_attrs["brand"] and gem_attrs["brand"] not in flip_low:
        return False
    # if gem has units/models, at least one must appear in flipkart name
    specifics = set()
    specifics.update(gem_attrs["units"])
    specifics.update(m.lower() for m in gem_attrs["models"])
    if specifics and not any(s in flip_low for s in specifics):
        # allow through if brand matched strongly and no specifics overlap?
        # No — per plan this is a hard filter to avoid false matches.
        return False
    return True


def load_products(con):
    cur = con.cursor()
    gem = list(cur.execute(
        "SELECT id, name, link, price FROM raw_products WHERE source='gem'"))
    market = list(cur.execute(
        "SELECT id, name, price FROM raw_products WHERE source='flipkart'"))
    return gem, market


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=75)
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    gem_products, market_products = load_products(con)
    if not gem_products or not market_products:
        print(f"need both sources loaded (gem={len(gem_products)}, flipkart={len(market_products)})")
        print("run scrapers then: python -m db.load_data")
        return 1

    gem_prep = []
    for gid, gname, glink, gprice in gem_products:
        slug = extract_slug(glink or "")
        enriched = f"{gname or ''} {slug}".strip()
        gem_prep.append((gid, gname, gprice, normalize(enriched), enriched))
    market_prep = [(mid, mname, mprice, normalize(mname or "")) for mid, mname, mprice in market_products]

    scores_seen = []
    matches = []
    now = datetime.now(timezone.utc).isoformat()

    for gid, gname, gprice, gen_norm, gen_enriched in gem_prep:
        g_attrs = extract_attributes(gen_enriched)
        for mid, mname, mprice, m_norm in market_prep:
            if not passes_filter(g_attrs, mname or ""):
                continue
            score = fuzz.token_sort_ratio(gen_norm, m_norm)
            scores_seen.append(score)
            if score >= args.threshold:
                matches.append((gid, gname, gprice, mid, mname, mprice, score))

    cur = con.cursor()
    cur.execute("DELETE FROM matched_products WHERE match_method='regex_filter+slug+fuzzy'")
    for gid, _, _, mid, _, _, score in matches:
        cur.execute(
            """INSERT INTO matched_products
               (gem_product_id, market_product_id, similarity_score, match_method, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (gid, mid, float(score), "regex_filter+slug+fuzzy", now),
        )
    con.commit()

    # summary table
    print(f"GeM products: {len(gem_products)} | Flipkart: {len(market_products)} | matches: {len(matches)}")
    if scores_seen:
        import statistics
        print(f"scored pairs: {len(scores_seen)} max={max(scores_seen):.1f} "
              f"median={statistics.median(scores_seen):.1f} mean={statistics.mean(scores_seen):.1f}")
    else:
        print("no pairs passed the hard filter — check brand/unit extraction")
    print("-" * 120)
    print(f"{'GeM name'[:40]:40} | {'GeM Rs':>10} | {'Flipkart name'[:40]:40} | {'FK Rs':>10} | {'Diff%':>7} | {'sim':>5}")
    print("-" * 120)
    for _, gname, gprice, _, mname, mprice, score in sorted(matches, key=lambda r: -r[6])[:50]:
        try:
            diff = (float(mprice) - float(gprice)) if gprice and mprice else None
            pct = (diff / float(gprice) * 100) if diff is not None and float(gprice) else None
        except (TypeError, ValueError):
            diff, pct = None, None
        pct_s = f"{pct:+.1f}%" if pct is not None else "n/a"
        print(f"{(gname or '')[:40]:40} | {str(gprice or 'n/a'):>10} | "
              f"{(mname or '')[:40]:40} | {str(mprice or 'n/a'):>10} | {pct_s:>7} | {score:5.1f}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
