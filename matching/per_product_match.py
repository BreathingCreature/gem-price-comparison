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
                                   extract_slug as slug_words, passes_filter,
                                   gates_pass)

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "db" / "gem_project.db"


def load_linked(con, gem_ids=None):
    """Return two structures:
    gem:   dict[id] = (name, link, price)
    fk:    dict[gem_id] -> list[(fkid, name, price, link)]
    """
    if gem_ids:
        qmarks = ",".join("?" * len(gem_ids))
        gem_rows = con.execute(
            f"SELECT id, name, link, price FROM raw_products "
            f"WHERE source='gem' AND id IN ({qmarks})", gem_ids).fetchall()
        fk_rows = con.execute(
            f"SELECT id, name, price, searched_for_gem_id, link FROM raw_products "
            f"WHERE source='flipkart' AND searched_for_gem_id IN ({qmarks})",
            gem_ids).fetchall()
    else:
        gem_rows = con.execute(
            "SELECT id, name, link, price FROM raw_products WHERE source='gem' "
            "AND id IN (SELECT DISTINCT searched_for_gem_id FROM raw_products "
            "WHERE searched_for_gem_id IS NOT NULL)").fetchall()
        fk_rows = con.execute(
            "SELECT id, name, price, searched_for_gem_id, link FROM raw_products "
            "WHERE source='flipkart' AND searched_for_gem_id IS NOT NULL").fetchall()
    gems = {r[0]: (r[1], r[2], r[3]) for r in gem_rows}
    fk = {}
    for fid, name, price, gid, link in fk_rows:
        fk.setdefault(gid, []).append((fid, name, price, link))
    return gems, fk


def score_product(gem_name, gem_link, candidates):
    """Fuzzy score one GeM product against its own candidates. Returns list of
    (score, name, price, fkid, link) sorted desc."""
    slug = slug_words(gem_link or "")
    enriched = f"{gem_name or ''} {slug}".strip()
    gen_norm = normalize(enriched)
    g_attrs = extract_attributes(enriched)
    out = []
    for fid, mname, mprice, mlink in candidates:
        if not passes_filter(g_attrs, mname or ""):
            continue
        s = fuzz.token_sort_ratio(gen_norm, normalize(mname or ""))
        out.append((s, mname, mprice, fid, mlink))
    out.sort(key=lambda r: -r[0])
    return out


def score_product_embed(gem_name, gem_link, candidates):
    """Embedding cosine score (0-1) of one GeM product vs ITS candidates.
    Hard regex filter still runs first; the similarity step is now embeddings."""
    from matching.match_engine import embed, cos_score
    slug = slug_words(gem_link or "")
    enriched = f"{gem_name or ''} {slug}".strip()
    gen_text = normalize(enriched)
    g_attrs = extract_attributes(enriched)
    passed = [c for c in candidates if passes_filter(g_attrs, c[1] or "")]
    if not passed:
        return []
    texts_b = [normalize(c[1] or "") for c in passed]
    sims = cos_score([gen_text] * len(passed), texts_b)
    out = [(sims[i], passed[i][1], passed[i][2], passed[i][0], passed[i][3])
           for i in range(len(passed))]
    out.sort(key=lambda r: -r[0])
    return out


def accept_with_gates(enriched, cos, fk_name, threshold, gem_link="", fk_link=""):
    """Cosine + model-token gate + pack gate + form-factor gate ->
    accept/reject decision dict."""
    g = gates_pass(enriched, fk_name, cos=cos, threshold=threshold,
                   gem_link=gem_link, flip_link=fk_link)
    return g


def _csv_in(x):
    return str(x or "").replace(";", ",")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=75)
    ap.add_argument("--gem-ids", default="")
    ap.add_argument("--dont-write", action="store_true")
    ap.add_argument("--scorer", choices=["fuzzy", "embedding"], default="fuzzy")
    ap.add_argument("--csv", default="", help="write machine-readable report to this path")
    args = ap.parse_args()

    con = sqlite3.connect(DB_PATH)
    gem_ids = [int(x) for x in args.gem_ids.split(",") if x.strip()] if args.gem_ids else None
    gems, linked = load_linked(con, gem_ids)
    if not gems:
        print("no GeM products with linked Flipkart rows — run"
              " `python -m scrapers.per_product_lookup --gem-ids ...` first.")
        return 1

    scorer = score_product_embed if args.scorer == "embedding" else score_product
    results = []   # (gid, best_score, best_fkid, gate_info)
    report_rows = []
    now = datetime.now(timezone.utc).isoformat()

    print(f"{'GEM name/slug':55} {'query':38} {'best cand (score)':50} {'FK Rs':>9} {'match?':>6}")
    print("-" * 170)
    for gid in sorted(gems):
        name, link, _ = gems[gid]
        cands = linked.get(gid, [])
        scored = scorer(name, link, cands)
        slug = raw_slug(link or "")
        enriched = f"{name or ''} {slug}".strip()
        queries = build_queries(name, link)
        queries_s = ", ".join(queries[:2]) if queries else "(none)"
        best = scored[0] if scored else None
        best_gate = None
        if args.scorer == "embedding" and best:
            best_gate = accept_with_gates(enriched, best[0], best[1], args.threshold,
                                          gem_link=link, fk_link=best[4])
            matched = best_gate["pass"]
        else:
            matched = best and best[0] >= args.threshold
        if matched:
            results.append((gid, best[3], best[0], best_gate))
        best_s = ""
        if best:
            best_s = f"{best[1][:48]:50} ({best[0]:.3f})"
        match_s = "YES" if matched else ("NO" if scored else "no-cand")
        # keep lines ascii-safe for cp1252 console
        print(f"{(name or '')[:40]:42} {'|' if False else ''} {queries_s[:40]:40}"
              f" {best_s:52} {str(best[2] if best else ''):>9} {match_s:>6}")

        for sn, mname, mprice, _, mlink in scored:
            gate = None
            if args.scorer == "embedding":
                gate = accept_with_gates(enriched, sn, mname, args.threshold,
                                         gem_link=link, fk_link=mlink)
            row = {
                "gem_id": gid,
                "gem_name": name,
                "gem_slug": slug,
                "gem_price": gems[gid][2],
                "query_primary": queries[0] if queries else "",
                "query_fallback": queries[1] if len(queries) > 1 else "",
                "fk_name": mname,
                "fk_price": mprice,
                "similarity": round(sn, 4),
                "scorer": args.scorer,
                "matched": "YES" if matched and sn == best[0] else "NO",
            }
            if gate:
                row.update({
                    "cosine_ok": gate["score_ok"],
                    "model_token_match": gate["model"],
                    "pack_match": gate["pack"],
                    "form_match": gate["form"],
                    "gem_form": gate["gem_form"],
                    "fk_form": gate["fk_form"],
                    "gem_tokens": " ".join(gate["gem_tokens"]),
                    "fk_tokens": " ".join(gate["fk_tokens"]),
                    "gem_pack": gate["gem_pack"],
                    "fk_pack": gate["fk_pack"],
                    "decision": "ACCEPT" if gate["pass"] else "REJECT",
                    "reason": gate["why"],
                })
            report_rows.append(row)

    if args.csv:
        import csv as csvlib
        FIELD_NAMES = ["gem_id", "gem_name", "gem_slug", "gem_price",
                       "query_primary", "query_fallback", "fk_name", "fk_price",
                       "similarity", "scorer", "matched",
"cosine_ok", "model_token_match", "pack_match", "form_match",
            "gem_form", "fk_form",
            "gem_tokens", "fk_tokens", "gem_pack", "fk_pack",
                       "decision", "reason"]
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csvlib.DictWriter(f, fieldnames=FIELD_NAMES)
            w.writeheader()
            w.writerows(report_rows)
        print(f"report written to {args.csv}")

    # write matches
    if not args.dont_write:
        method = "embedding_cosine+gates" if args.scorer == "embedding" else "per-product+slug+fuzzy"
        cur = con.cursor()
        cur.execute("DELETE FROM matched_products WHERE match_method=?", (method,))
        for gid, fkid, score, gate in results:
            cur.execute(
                "INSERT INTO matched_products (gem_product_id, market_product_id, similarity_score, cosine_score, model_token_match, pack_match, form_match, match_method, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (gid, fkid, float(score), float(score),
                 1 if gate and gate["model"] else 0, 1 if gate and gate["pack"] else 0,
                 1 if gate and gate["form"] else 0,
                 method, now))
        con.commit()
    con.close()

    print(f"\nproducts scored: {len(gems)} | matches above {args.threshold:g}: {len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())