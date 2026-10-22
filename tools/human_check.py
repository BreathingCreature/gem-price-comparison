"""Build the human-check sheet for one eval run.

For every confirmed match in the run, print one row: product, shop, listing
title, URL, price, the LLM's confidence and its reason. A person can then
mark "same product? yes/no" without opening the trace JSON.

Usage:
    python tools/human_check.py eval/results/fixed2.jsonl --out docs/human_check_sheet.md
    python tools/human_check.py eval/results/fixed2.jsonl --price-unknown   # only the new kind

The sheet says nothing about correctness by itself: it is the list to look at.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path


def _trace_for(record: dict, folder: str = "traces") -> dict:
    trace_id = record.get("trace_id")
    if not trace_id:
        return {}
    path = Path(folder) / f"{trace_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def rows_for(record: dict, trace: dict, *, only_unknown_price: bool = False) -> list[dict]:
    stages = (trace or {}).get("stages") or {}
    candidates = {}
    for key in ("amazon_search", "flipkart_search"):
        for batch in stages.get(key) or []:
            for candidate in batch.get("candidates") or []:
                candidates[candidate.get("url")] = candidate
    for candidate in stages.get("generic_scrapes") or []:
        candidates[candidate.get("url")] = candidate
    gem = (stages.get("extraction") or {}).get("title") or record.get("id")
    rows = []
    for decision in (stages.get("matching") or {}).get("all_decisions") or []:
        if decision.get("outcome") != "match":
            continue
        price = decision.get("price_used")
        if only_unknown_price and price is not None:
            continue
        url = decision.get("candidate_url") or ""
        candidate = candidates.get(url) or {}
        fields = candidate.get("structured_fields") or {}
        rows.append(
            {
                "id": record.get("id"),
                "gem": gem,
                "title": fields.get("title") or "",
                "url": url,
                "price": price,
                "confidence": decision.get("confidence"),
                "reason": decision.get("reason") or "",
                "same_product": "",
            }
        )
    return rows


def _escape(text: str) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def render(rows: list[dict]) -> str:
    lines = [
        "# Human-check sheet",
        "",
        "Every confirmed match in this run. Mark the last column by hand: is it",
        "really the same product? A row with no price is a confirmed identity",
        "from the listing itself (model code / brand + two words).",
        "",
        "| # | product | shop listing | URL | price | conf | LLM reason | same product? |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, row in enumerate(rows, 1):
        price = f"{row['price']:.0f}" if isinstance(row["price"], (int, float)) else "unknown"
        lines.append(
            f"| {i} | {_escape(row['id'])} | {_escape(row['title'][:70])} | "
            f"{_escape(row['url'])[:90]} | {price} | {row['confidence']} | "
            f"{_escape(row['reason'])[:60]} |  |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", help="eval/results/<label>.jsonl")
    ap.add_argument("--out", help="Markdown file to write (default: stdout)")
    ap.add_argument("--price-unknown", action="store_true", help="only confirmed matches with no price")
    ap.add_argument("--traces", default="traces")
    args = ap.parse_args(argv)

    records = []
    for path in glob.glob(args.results):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    print(f"skipped an unreadable line in {path}", file=sys.stderr)
    rows = []
    for record in records:
        rows.extend(rows_for(record, _trace_for(record, args.traces), only_unknown_price=args.price_unknown))
    text = render(rows)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{len(rows)} confirmed match(es) written to {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
