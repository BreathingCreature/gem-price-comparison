"""Score a run of the pipeline against a hand-made answer key.

    python -m eval.score --check-key eval/answer_key.csv
    python -m eval.score --key eval/answer_key.csv --results eval/results/before.jsonl
    python -m eval.score --key ... --results ... --json eval/results/before.score.json

The answer key (CSV) has one row per GeM product. For each shop column a
person has looked at the shop by hand:

    blank     not checked, this shop is ignored for this product
    NONE      checked, the product is not sold there
    URL       checked, this is the same product (several URLs separated by
              a space or | mean "any of these is right")

Columns: id, gem_url, gem_name, branded (yes/no), gem_price, amazon_url,
amazon_price, flipkart_url, flipkart_price, other_urls, notes.
other_urls holds listings on any other shop, one per URL, and each URL is
its own thing to find.

A results file (JSON lines, written by eval/run_eval.py) has one line per
product with a "matches" list. See the README in docs/evaluation.md for the
exact meaning of each number.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from pathlib import Path
from urllib.parse import urlparse

from core.keys import listing_key

PRICE_TOLERANCE = 0.02  # a price counts as right when it is within 2 percent
NONE_WORD = "NONE"
KEY_COLUMNS = [
    "id", "gem_url", "gem_name", "branded", "gem_price",
    "amazon_url", "amazon_price", "flipkart_url", "flipkart_price", "other_urls", "notes",
]


# --- reading the files ----------------------------------------------------------

def load_key(path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [{k: (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(f)]
    return [r for r in rows if r.get("id") or r.get("gem_url")]


def load_results(path) -> dict[str, dict]:
    """id -> result record. A later line for the same id wins."""
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                out[str(rec["id"])] = rec
    return out


# --- URLs -----------------------------------------------------------------------

def site_of(url: str) -> str:
    """'amazon', 'flipkart', or the plain host name for anything else."""
    host = (urlparse(url if "//" in url else "//" + url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if host.endswith("amazon.in") or host.endswith("amazon.com"):
        return "amazon"
    if host.endswith("flipkart.com"):
        return "flipkart"
    return host


def url_key(url: str) -> str:
    """The part of a listing URL that identifies the listing.

    Delegates to core.keys.listing_key: the scorer and the pipeline must
    agree on what "the same listing" means, or a run can dedup two listings
    that the answer key counts separately (and the other way round)."""
    return listing_key(url)


def parse_cell(cell: str):
    """('unchecked'|'none'|'urls', [urls])"""
    cell = (cell or "").strip()
    if not cell:
        return "unchecked", []
    if cell.upper() == NONE_WORD:
        return "none", []
    return "urls", [u for u in re.split(r"[\s|]+", cell) if u]


def to_price(text) -> float | None:
    if text is None or text == "":
        return None
    try:
        return float(str(text).replace(",", "").replace("₹", "").strip())
    except ValueError:
        return None


# --- checking the key itself ----------------------------------------------------

def check_key(rows: list[dict]) -> tuple[list[str], list[str]]:
    """(problems, notes). Problems should be fixed before scoring."""
    problems, notes = [], []
    seen: set[str] = set()
    checked_slots = 0
    for n, r in enumerate(rows, start=2):  # line 1 is the header
        rid = r.get("id", "")
        where = f"line {n} (id {rid or '?'})"
        if not rid:
            problems.append(f"{where}: id is empty")
        elif rid in seen:
            problems.append(f"{where}: id used twice")
        seen.add(rid)
        if not r.get("gem_url", "").startswith("http"):
            problems.append(f"{where}: gem_url must start with http")
        if r.get("branded", "").lower() not in ("yes", "no"):
            problems.append(f"{where}: branded must be yes or no")
        any_checked = False
        for col, price_col in (("amazon_url", "amazon_price"), ("flipkart_url", "flipkart_price")):
            state, urls = parse_cell(r.get(col, ""))
            if state != "unchecked":
                any_checked = True
                checked_slots += 1
            for u in urls:
                if not u.startswith("http"):
                    problems.append(f"{where}: {col} has a value that is not a URL: {u[:40]}")
                elif site_of(u) != col.split("_")[0]:
                    problems.append(f"{where}: {col} points at {site_of(u)}, not {col.split('_')[0]}")
            if state == "urls" and to_price(r.get(price_col)) is None:
                notes.append(f"{where}: {price_col} is empty, so price accuracy is skipped for this one")
        state, urls = parse_cell(r.get("other_urls", ""))
        if state != "unchecked":
            any_checked = True
            checked_slots += 1
        for u in urls:
            if not u.startswith("http"):
                problems.append(f"{where}: other_urls has a value that is not a URL: {u[:40]}")
            elif site_of(u) in ("amazon", "flipkart"):
                problems.append(f"{where}: other_urls holds an Amazon or Flipkart link, put it in its own column")
        if not any_checked:
            problems.append(f"{where}: no shop was checked, the row cannot be scored")
    notes.append(f"{len(rows)} products, {checked_slots} shop checks recorded")
    return problems, notes


# --- scoring --------------------------------------------------------------------

def _slots(row: dict):
    """The things to find for one product, as a list of dicts."""
    slots = []
    for site, col, price_col in (("amazon", "amazon_url", "amazon_price"), ("flipkart", "flipkart_url", "flipkart_price")):
        state, urls = parse_cell(row.get(col, ""))
        if state == "unchecked":
            continue
        slots.append({"site": site, "state": state, "keys": {url_key(u) for u in urls}, "price": to_price(row.get(price_col))})
    state, urls = parse_cell(row.get("other_urls", ""))
    if state != "unchecked":
        if state == "none":
            slots.append({"site": "other", "state": "none", "keys": set(), "price": None, "one": None})
        for u in urls:
            slots.append({"site": "other", "state": "urls", "keys": {url_key(u)}, "price": None, "one": site_of(u)})
    return slots


def score_row(row: dict, record: dict) -> dict:
    """Outcome for one product.

    found:   slots that had an answer and were found
    wanted:  slots that had an answer
    preds / correct: predictions on checked shops, and how many were right
    price_checked / price_ok: for found Amazon and Flipkart slots with a known price
    """
    slots = _slots(row)
    other_checked = any(s["site"] == "other" for s in slots)
    matches = list(record.get("matches") or [])
    preds = []  # (site, key, price, url)
    for m in matches:
        url = m.get("url") or ""
        site = site_of(url)
        group = site if site in ("amazon", "flipkart") else "other"
        if group == "other" and not other_checked:
            continue  # nobody looked at other shops for this product, so the answer cannot be judged
        if group in ("amazon", "flipkart") and not any(s["site"] == group for s in slots):
            continue
        preds.append((group, url_key(url), to_price(m.get("price")), site))

    wanted = found = price_checked = price_ok = 0
    correct_pred_idx: set[int] = set()
    for s in slots:
        if s["state"] != "urls":
            continue
        wanted += 1
        hits = [i for i, p in enumerate(preds) if p[0] == s["site"] and p[1] in s["keys"]]
        if hits:
            found += 1
            correct_pred_idx.update(hits)
            if s["site"] in ("amazon", "flipkart") and s["price"]:
                price_checked += 1
                best = min((preds[i][2] for i in hits if preds[i][2] is not None), default=None)
                if best is not None and abs(best - s["price"]) <= PRICE_TOLERANCE * s["price"]:
                    price_ok += 1
    # other-shop predictions that match any "other" truth key also count as correct
    other_keys = set().union(*[s["keys"] for s in slots if s["site"] == "other"]) if other_checked else set()
    for i, p in enumerate(preds):
        if p[0] == "other" and p[1] in other_keys:
            correct_pred_idx.add(i)

    per_site = {}
    for s in slots:
        if s["state"] == "urls":
            per_site.setdefault(s["site"], [0, 0])
            per_site[s["site"]][1] += 1
    for site in per_site:
        for s in slots:
            if s["site"] == site and s["state"] == "urls":
                if any(p[0] == site and p[1] in s["keys"] for p in preds):
                    per_site[site][0] += 1
    return {
        "id": row.get("id"),
        "branded": row.get("branded", "").lower() == "yes",
        "wanted": wanted,
        "found": found,
        "preds": len(preds),
        "correct": len(correct_pred_idx),
        "price_checked": price_checked,
        "price_ok": price_ok,
        "per_site": per_site,
        "seconds": record.get("seconds"),
        "error": record.get("error"),
        "missing": record.get("_missing", False),
    }


def _ratio(a: float, b: float):
    return round(a / b, 4) if b else None


def _f1(p, r):
    if p is None or r is None or (p + r) == 0:
        return None
    return round(2 * p * r / (p + r), 4)


def summarize(outcomes: list[dict]) -> dict:
    wanted = sum(o["wanted"] for o in outcomes)
    found = sum(o["found"] for o in outcomes)
    preds = sum(o["preds"] for o in outcomes)
    correct = sum(o["correct"] for o in outcomes)
    pc = sum(o["price_checked"] for o in outcomes)
    pk = sum(o["price_ok"] for o in outcomes)
    recall, precision = _ratio(found, wanted), _ratio(correct, preds)
    sites: dict[str, list[int]] = {}
    for o in outcomes:
        for site, (f, w) in o["per_site"].items():
            sites.setdefault(site, [0, 0])
            sites[site][0] += f
            sites[site][1] += w
    times = sorted(o["seconds"] for o in outcomes if isinstance(o.get("seconds"), (int, float)) and not o.get("error"))
    p90 = times[min(len(times) - 1, int(round(0.9 * (len(times) - 1))))] if times else None
    return {
        "products": len(outcomes),
        "errors": sum(1 for o in outcomes if o.get("error")),
        "not_run": sum(1 for o in outcomes if o.get("missing")),
        "slots_wanted": wanted,
        "slots_found": found,
        "recall": recall,
        "predictions": preds,
        "predictions_correct": correct,
        "precision": precision,
        "f1": _f1(precision, recall),
        "recall_by_site": {s: _ratio(f, w) for s, (f, w) in sorted(sites.items())},
        "price_checked": pc,
        "price_within_2pct": pk,
        "price_accuracy": _ratio(pk, pc),
        "seconds_median": round(statistics.median(times), 1) if times else None,
        "seconds_p90": round(p90, 1) if p90 is not None else None,
    }


def score(rows: list[dict], results: dict[str, dict]) -> dict:
    outcomes = []
    for row in rows:
        rec = results.get(row.get("id", ""))
        if rec is None:
            rec = {"matches": [], "_missing": True}
        outcomes.append(score_row(row, rec))
    return {
        "all": summarize(outcomes),
        "branded": summarize([o for o in outcomes if o["branded"]]),
        "generic": summarize([o for o in outcomes if not o["branded"]]),
        "per_product": outcomes,
    }


# --- printing -------------------------------------------------------------------

def _pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def render_markdown(result: dict) -> str:
    lines = ["| Group | Products | Recall | Precision | F1 | Price within 2% | Median s | p90 s | Errors |", "|---|---|---|---|---|---|---|---|---|"]
    for name in ("all", "branded", "generic"):
        s = result[name]
        lines.append(
            f"| {name} | {s['products']} | {_pct(s['recall'])} ({s['slots_found']}/{s['slots_wanted']}) "
            f"| {_pct(s['precision'])} ({s['predictions_correct']}/{s['predictions']}) | {_pct(s['f1'])} "
            f"| {_pct(s['price_accuracy'])} ({s['price_within_2pct']}/{s['price_checked']}) "
            f"| {s['seconds_median'] if s['seconds_median'] is not None else 'n/a'} "
            f"| {s['seconds_p90'] if s['seconds_p90'] is not None else 'n/a'} | {s['errors']} |"
        )
    by_site = result["all"]["recall_by_site"]
    if by_site:
        lines.append("")
        lines.append("Recall by shop: " + ", ".join(f"{k} {_pct(v)}" for k, v in by_site.items()))
    if result["all"]["not_run"]:
        lines.append(f"Warning: {result['all']['not_run']} products from the key have no line in the results file.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-key", metavar="CSV", help="only check the answer key and print problems")
    ap.add_argument("--key", metavar="CSV")
    ap.add_argument("--results", metavar="JSONL")
    ap.add_argument("--json", metavar="FILE", help="also write the full numbers here")
    args = ap.parse_args(argv)

    if args.check_key:
        rows = load_key(args.check_key)
        problems, notes = check_key(rows)
        for n in notes:
            print("note:", n)
        for p in problems:
            print("PROBLEM:", p)
        print(f"{len(problems)} problem(s)")
        return 1 if problems else 0

    if not (args.key and args.results):
        ap.print_usage()
        return 2
    rows = load_key(args.key)
    problems, _ = check_key(rows)
    if problems:
        print("The answer key has problems, fix them first (python -m eval.score --check-key ...):")
        for p in problems[:10]:
            print(" -", p)
        return 1
    result = score(rows, load_results(args.results))
    print(render_markdown(result))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nFull numbers written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
