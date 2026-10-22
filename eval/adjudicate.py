"""Check the matches a run found that are NOT in the answer key (pooling).

Why: the key lists the listings one person found by hand. A run can find a
different listing that is also the same product (another seller, another
colour). The strict scorer counts that as wrong. This tool lets a person
look at those extra listings and say yes or no, then writes a bigger key.
Both scores should be reported: strict (original key) and pooled.

    # 1. list the matches that the key does not know
    python -m eval.adjudicate review --key eval/answer_key.csv \\
        --results eval/results/fixed2.jsonl --out eval/results/fixed2.review.csv

    # 2. open the CSV, fill the "verdict" column with yes / no / skip
    #    (open each link; yes = the same brand, model, size and pack)

    # 3. write the pooled key
    python -m eval.adjudicate pool --key eval/answer_key.csv \\
        --review eval/results/fixed2.review.csv --out eval/answer_key_pooled.csv

Rules that keep this honest: only a person's yes is added; a row that says
yes for a shop the key marked NONE is reported, not silently changed; the
original key file is never edited.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from eval.score import KEY_COLUMNS, load_key, load_results, parse_cell, site_of, url_key

REVIEW_COLUMNS = ["id", "gem_name", "gem_url", "site", "url", "price", "confidence", "verdict", "note"]
YES = {"yes", "y", "true", "1"}
NO = {"no", "n", "false", "0"}


def _column_for(url: str) -> str:
    site = site_of(url)
    return {"amazon": "amazon_url", "flipkart": "flipkart_url"}.get(site, "other_urls")


def extra_matches(rows: list[dict], results: dict[str, dict]) -> list[dict]:
    """Matches in the results whose listing is not already in the key."""
    out = []
    for row in rows:
        rec = results.get(row.get("id", ""))
        if not rec:
            continue
        known = set()
        for col in ("amazon_url", "flipkart_url", "other_urls"):
            known.update(url_key(u) for u in parse_cell(row.get(col, ""))[1])
        seen = set()
        for m in rec.get("matches") or []:
            url = m.get("url") or ""
            k = url_key(url)
            if not k or k in known or k in seen:
                continue
            seen.add(k)
            out.append({
                "id": row.get("id", ""), "gem_name": row.get("gem_name", ""), "gem_url": row.get("gem_url", ""),
                "site": site_of(url), "url": url, "price": m.get("price", ""),
                "confidence": m.get("confidence", ""), "verdict": "", "note": "",
            })
    return out


def write_review(path, items: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_COLUMNS)
        w.writeheader()
        w.writerows(items)


def pool(rows: list[dict], review: list[dict]) -> tuple[list[dict], list[str]]:
    """New key rows with every 'yes' listing added. Also returns a plain report."""
    by_id = {r["id"]: dict(r) for r in rows}
    report = []
    added = 0
    for item in review:
        verdict = (item.get("verdict") or "").strip().lower()
        if verdict not in YES:
            continue
        row = by_id.get(item["id"])
        if row is None:
            report.append(f"{item['id']}: not in the key, skipped")
            continue
        url = item["url"].strip()
        col = _column_for(url)
        state, urls = parse_cell(row.get(col, ""))
        if state == "none":
            report.append(f"{item['id']}: the key said NONE for {col} but you said yes to {url[:60]} -- the key is changed, "
                          f"please check that you meant it")
            urls = []
        if url_key(url) in {url_key(u) for u in urls}:
            continue
        row[col] = " | ".join(urls + [url])
        added += 1
    report.append(f"{added} listing(s) added to the key")
    out = []
    for r in rows:
        out.append(by_id[r["id"]])
    return out, report


def write_key(path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=KEY_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in KEY_COLUMNS})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("review")
    a.add_argument("--key", required=True)
    a.add_argument("--results", required=True)
    a.add_argument("--out", required=True)
    b = sub.add_parser("pool")
    b.add_argument("--key", required=True)
    b.add_argument("--review", required=True)
    b.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    rows = load_key(args.key)
    if args.cmd == "review":
        items = extra_matches(rows, load_results(args.results))
        write_review(args.out, items)
        print(f"{len(items)} extra match(es) written to {args.out}. Fill the verdict column with yes / no.")
        return 0
    with open(args.review, newline="", encoding="utf-8-sig") as f:
        review = list(csv.DictReader(f))
    new_rows, report = pool(rows, review)
    if Path(args.out).resolve() == Path(args.key).resolve():
        print("Refusing to overwrite the original key; choose another --out.")
        return 2
    write_key(args.out, new_rows)
    for line in report:
        print(line)
    print(f"pooled key written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
