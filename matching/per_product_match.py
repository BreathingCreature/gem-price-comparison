"""Per-product matching v2: category-aware embedding + soft gates.

For each GeM product, score against its linked Flipkart candidates using
embedding cosine + soft gates (brand, model tokens, pack, form factor).
No hard brand filter — brand is a signal, not a gate.

Usage:
    python -m matching.per_product_match --threshold 0.6 --scorer embedding
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
                                   extract_slug as slug_words,
                                   gates_pass, composite_score,
                                   detect_category, build_queries_for_category,
                                   soft_attribute_overlap)
from matching.llm_attributes import get_product_attributes

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


def score_product_embed_v2(gem_name, gem_link, candidates, con):
    """Embedding cosine score with category-aware soft gates.
    Returns list of (composite_score, cosine, name, price, fkid, link, gates_dict) sorted desc."""
    from matching.match_engine import embed, cos_score
    
    # Get LLM attributes for this product
    llm_attrs = get_product_attributes(gem_name, gem_link, con)
    
    # Detect category
    category = detect_category(gem_name, gem_link, llm_attrs)
    
    slug = slug_words(gem_link or "")
    enriched = f"{gem_name or ''} {slug}".strip()
    gen_text = normalize(enriched)
    
    # Extract regex attributes, override brand with LLM
    g_attrs = extract_attributes(enriched)
    if llm_attrs.get("brand"):
        g_attrs["brand"] = llm_attrs["brand"].lower()
    
    # Soft filter - score but don't reject
    candidate_scores = []
    for fid, mname, mprice, mlink in candidates:
        attr_score = soft_attribute_overlap(g_attrs, mname or "")
        candidate_scores.append((fid, mname, mprice, mlink, attr_score))
    
    # Sort by attribute overlap first to prioritize promising candidates
    candidate_scores.sort(key=lambda x: -x[4])
    
    # Compute embeddings for top candidates (limit to avoid excessive compute)
    top_candidates = candidate_scores[:10]
    if not top_candidates:
        return []
    
    fk_texts = [normalize(c[1] or "") for c in top_candidates]
    cos_sims = cos_score([gen_text] * len(fk_texts), fk_texts)
    
    # Score with gates and composite
    out = []
    for (fid, mname, mprice, mlink, attr_score), cos in zip(top_candidates, cos_sims):
        gates = gates_pass(enriched, mname, category.category, cos, 
                          gem_link=gem_link, flip_link=mlink)
        comp = composite_score(cos, gates, category.category)
        out.append((comp, cos, mname, mprice, fid, mlink, gates, attr_score, category.category))
    
    out.sort(key=lambda r: -r[0])
    return out


def accept_with_gates(enriched, cos, fk_name, threshold, gem_link="", fk_link="", category="unknown"):
    """Cosine + model-token gate + pack gate + form-factor gate ->
    accept/reject decision dict with category awareness."""
    g = gates_pass(enriched, fk_name, category, cos=cos, threshold=threshold,
                   gem_link=gem_link, flip_link=fk_link)
    return g


def _csv_in(x):
    return str(x or "").replace(";", ",")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.6, help="Composite score threshold (0-1)")
    ap.add_argument("--gem-ids", default="")
    ap.add_argument("--dont-write", action="store_true")
    ap.add_argument("--scorer", choices=["fuzzy", "embedding", "embedding_v2"], default="embedding_v2")
    ap.add_argument("--csv", default="", help="write machine-readable report to this path")
    args = ap.parse_args()

    con = sqlite3.connect(DB_PATH)
    gem_ids = [int(x) for x in args.gem_ids.split(",") if x.strip()] if args.gem_ids else None
    gems, linked = load_linked(con, gem_ids)
    if not gems:
        print("no GeM products with linked Flipkart rows — run"
              " `python -m scrapers.per_product_lookup --gem-ids ...` first.")
        return 1

    results = []   # (gid, best_fkid, best_composite, best_cosine, gate_info)
    report_rows = []
    now = datetime.now(timezone.utc).isoformat()

    print(f"{'GEM name/slug':55} {'query':38} {'best cand (score)':50} {'FK Rs':>9} {'match?':>6}")
    print("-" * 170)
    for gid in sorted(gems):
        name, link, _ = gems[gid]
        cands = linked.get(gid, [])
        
        if args.scorer == "embedding_v2":
            scored = score_product_embed_v2(name, link, cands, con)
        elif args.scorer == "embedding":
            from matching.match_engine import embed, cos_score
            slug = slug_words(link or "")
            enriched = f"{name or ''} {slug}".strip()
            gen_text = normalize(enriched)
            llm_attrs = get_product_attributes(name, link, con)
            g_attrs = extract_attributes(enriched)
            if llm_attrs.get("brand"):
                g_attrs["brand"] = llm_attrs["brand"].lower()
            passed = [c for c in cands if soft_attribute_overlap(g_attrs, c[1] or "") > 0]
            if not passed:
                scored = []
            else:
                texts_b = [normalize(c[1] or "") for c in passed]
                cos_sims = cos_score([gen_text] * len(passed), texts_b)
                scored = [(cos_sims[i], passed[i][1], passed[i][2], passed[i][0], passed[i][3])
                          for i in range(len(passed))]
                scored.sort(key=lambda r: -r[0])
        else:
            # legacy fuzzy
            from matching.match_engine import extract_attributes, extract_slug as sw, passes_filter
            g_attrs = _hard_filter_attrs(name, link, con)
            slug = raw_slug(link or "")
            enriched = f"{name or ''} {slug}".strip()
            gen_norm = normalize(enriched)
            scored = []
            for fid, mname, mprice, mlink in cands:
                if not passes_filter(g_attrs, mname or ""):
                    continue
                s = fuzz.token_sort_ratio(gen_norm, normalize(mname or ""))
                scored.append((s, mname, mprice, fid, mlink))
            scored.sort(key=lambda r: -r[0])
        
        slug = raw_slug(link or "")
        enriched = f"{name or ''} {slug}".strip()
        queries = build_queries(name, link)
        queries_s = ", ".join(queries[:2]) if queries else "(none)"
        best = scored[0] if scored else None
        best_gate = None
        
        if args.scorer == "embedding_v2" and best:
            best_gate = best[6]  # gates dict
            matched = best[0] >= args.threshold and best_gate["pass"]
        elif args.scorer == "embedding" and best:
            llm_attrs = get_product_attributes(name, link, con)
            cat = detect_category(name, link, llm_attrs).category
            best_gate = accept_with_gates(enriched, best[0], best[1], args.threshold,
                                          gem_link=link, fk_link=best[4], category=cat)
            matched = best_gate["pass"]
        else:
            matched = best and best[0] >= args.threshold
        
        if matched:
            if args.scorer == "embedding_v2":
                results.append((gid, best[4], best[0], best[1], best_gate))
            else:
                results.append((gid, best[3], best[0], best_gate))
        
        best_s = ""
        if best:
            if args.scorer == "embedding_v2":
                best_s = f"{best[2][:48]:50} (comp={best[0]:.3f}, cos={best[1]:.3f})"
            else:
                best_s = f"{best[1][:48]:50} ({best[0]:.3f})"
        match_s = "YES" if matched else ("NO" if scored else "no-cand")
        print(f"{(name or '')[:40]:42} {queries_s[:40]:40}"
              f" {best_s:52} {str(best[2] if best and args.scorer=='embedding_v2' else (best[2] if best else '')):>9} {match_s:>6}")

        for item in scored:
            if args.scorer == "embedding_v2":
                comp, cos, mname, mprice, _, mlink, gate, attr_score, cat = item
                row = {
                    "gem_id": gid,
                    "gem_name": name,
                    "gem_slug": slug,
                    "gem_price": gems[gid][2],
                    "query_primary": queries[0] if queries else "",
                    "query_fallback": queries[1] if len(queries) > 1 else "",
                    "fk_name": mname,
                    "fk_price": mprice,
                    "similarity": round(comp, 4),
                    "cosine_similarity": round(cos, 4),
                    "attribute_overlap": round(attr_score, 3),
                    "scorer": args.scorer,
                    "matched": "YES" if (matched and comp == best[0]) else "NO",
                }
            else:
                sn, mname, mprice, _, mlink = item
                if args.scorer == "embedding":
                    llm_attrs = get_product_attributes(name, link, con)
                    cat = detect_category(name, link, llm_attrs).category
                    gate = accept_with_gates(enriched, sn, mname, args.threshold,
                                             gem_link=link, fk_link=mlink, category=cat)
                else:
                    gate = None
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
                    "matched": "YES" if (matched and sn == best[0]) else "NO",
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
                    "category": gate.get("category", "unknown"),
                })
            report_rows.append(row)

    if args.csv:
        import csv as csvlib
        FIELD_NAMES = ["gem_id", "gem_name", "gem_slug", "gem_price",
                       "query_primary", "query_fallback", "fk_name", "fk_price",
                       "similarity", "cosine_similarity", "attribute_overlap",
                       "scorer", "matched",
                       "cosine_ok", "model_token_match", "pack_match", "form_match",
                       "gem_form", "fk_form",
                       "gem_tokens", "fk_tokens", "gem_pack", "fk_pack",
                       "decision", "reason", "category"]
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csvlib.DictWriter(f, fieldnames=FIELD_NAMES)
            w.writeheader()
            w.writerows(report_rows)
        print(f"report written to {args.csv}")

    # write matches
    if not args.dont_write:
        method = "embedding_cosine+gates_v2" if args.scorer == "embedding_v2" else \
                 "embedding_cosine+gates" if args.scorer == "embedding" else \
                 "per-product+slug+fuzzy"
        cur = con.cursor()
        cur.execute("DELETE FROM matched_products WHERE match_method=?", (method,))
        for item in results:
            if args.scorer == "embedding_v2":
                gid, fkid, comp, cos, gate = item
                score = comp
            else:
                gid, fkid, score, gate = item
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