"""A simple baseline with no LLM, to compare the pipeline against.

It reuses the listings the pipeline already collected (they are saved in each
run's trace file) and decides by title similarity alone: for every shop it
keeps the single most similar listing if the similarity is at least the
threshold.

    python -m eval.baseline_fuzzy --results eval/results/before.jsonl \
        --out eval/results/fuzzy.jsonl [--threshold 0.5] [--price-band]

The output has the same format as the pipeline's results file, so it is
scored with the same command:  python -m eval.score --key ... --results eval/results/fuzzy.jsonl

Similarity is the Dice score of the word sets of the two titles:
2 * (words in both) / (words in A + words in B). It is 1.0 for the same
words and 0.0 for no shared words. No packages needed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_THRESHOLD = 0.5
BAND_LOW, BAND_HIGH = 0.3, 3.0  # used only with --price-band
CANDIDATE_STAGES = ("flipkart_search", "amazon_search", "flipkart_search_retry", "amazon_search_retry")


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def similarity(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return 2 * len(wa & wb) / (len(wa) + len(wb))


def _price(value):
    try:
        return float(str(value).replace(",", "").replace("₹", ""))
    except (TypeError, ValueError):
        return None


def candidates_from_trace(trace: dict) -> list[dict]:
    """Every listing the run collected, as {url, domain, title, price}."""
    stages = trace.get("stages") or {}
    out, seen = [], set()
    groups = []
    for name in CANDIDATE_STAGES:
        for search in stages.get(name) or []:
            groups.append(search.get("candidates") or [])
    groups.append(stages.get("generic_scrapes") or [])
    for d in stages.get("discovery") or []:
        groups.append(d.get("candidates") or [] if isinstance(d, dict) else [])
    for group in groups:
        for c in group:
            if not isinstance(c, dict):
                continue
            fields = c.get("structured_fields") or {}
            url = c.get("url")
            if not url or url in seen or not isinstance(fields, dict) or not fields.get("title"):
                continue
            seen.add(url)
            out.append({"url": url, "domain": c.get("source_domain") or "", "title": fields.get("title"), "price": _price(fields.get("price"))})
    return out


def pick_matches(gem_title: str, gem_price, candidates: list[dict], *, threshold=DEFAULT_THRESHOLD, price_band=False) -> list[dict]:
    """The best listing per shop, if it is similar enough."""
    best: dict[str, tuple[float, dict]] = {}
    for c in candidates:
        if price_band and gem_price and c["price"] is not None:
            if not (gem_price * BAND_LOW <= c["price"] <= gem_price * BAND_HIGH):
                continue
        score = similarity(gem_title, c["title"])
        if score >= threshold and (c["domain"] not in best or score > best[c["domain"]][0]):
            best[c["domain"]] = (score, c)
    return [
        {"url": c["url"], "domain": domain, "price": c["price"], "confidence": round(score, 3)}
        for domain, (score, c) in sorted(best.items())
    ]


def load_trace(traces_dir: Path, trace_id: str):
    path = traces_dir / f"{trace_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, help="results file of a pipeline run (has a trace_id per line)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--traces", default="traces", help="folder with the trace files")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--price-band", action="store_true", help="also drop listings priced outside 0.3x-3x of the GeM price")
    args = ap.parse_args(argv)

    out_lines, missing = [], 0
    with open(args.results, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            trace = load_trace(Path(args.traces), rec.get("trace_id") or "")
            if trace is None:
                missing += 1
                out_lines.append({"id": rec["id"], "matches": [], "error": "trace file not found", "seconds": rec.get("seconds")})
                continue
            gem = (trace.get("stages") or {}).get("extraction") or {}
            matches = pick_matches(
                gem.get("title") or "", _price(gem.get("gem_price")), candidates_from_trace(trace),
                threshold=args.threshold, price_band=args.price_band,
            )
            out_lines.append({"id": rec["id"], "matches": matches, "error": None, "seconds": rec.get("seconds")})
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out_lines), encoding="utf-8")
    print(f"wrote {len(out_lines)} lines to {args.out} ({missing} without a trace file)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
