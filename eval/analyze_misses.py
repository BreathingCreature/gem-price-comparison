"""Why every real match that was missed was missed, and what N would be safe.

    python -m eval.analyze_misses --key eval/answer_key.csv --results eval/results/baseline.jsonl

For each checked shop slot of the answer key that has a URL (the real match)
but is not in the results file, this walks the run's trace and says what
actually happened to that listing:

    never found        no search returned it
    fetch failed       it was returned, but its page would not load
    cut by filter      the prefilter dropped it before the LLM saw it
    LLM said no        the model judged it a different product
    safety gate        the price/currency gate hard-rejected it
    below threshold    the model matched it, but not confidently enough
    no price           matched, but no price could be extracted
    verify incomplete  the verdict never came back (LLM failure)

The listing's shop rank among the prefilter's dropped entries then powers the
"safe N" table: a dropped listing with rank R was kept whenever N >= R, so
the table reads "raising GEM_PREFILTER_TOP to N would put X of the missed
matches back in front of the LLM".
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.config import MATCH_CONFIDENCE_THRESHOLD
from eval.score import _slots as key_slots
from eval.score import load_key, load_results, site_of, url_key

DEFAULT_TRACES_DIR = "traces"
SAFE_N_CHOICES = (5, 10, 15, 20, 30, 50)

REASON_LABELS = {
    "never_found": "never found",
    "fetch_failed": "fetch failed",
    "cut_by_filter": "cut by filter",
    "llm_said_no": "LLM said no",
    "safety_gate": "safety gate",
    "below_threshold": "below confidence threshold",
    "no_price_demoted": "matched but no price",
    "verify_incomplete": "verify incomplete",
    "unexplained": "unexplained",
    "no_trace": "no trace file",
    "no_results": "not in the results file",
}


def _pct(value) -> str:
    return "?" if value is None else f"{value * 100:.0f}%"


def find_trace(traces_dir, trace_id) -> dict | None:
    if not trace_id:
        return None
    path = Path(traces_dir) / f"{trace_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def candidate_entries(trace: dict) -> list[dict]:
    """Every candidate dict any search or scrape returned in this run."""
    stages = trace.get("stages") or {}
    entries: list = []
    for name in ("flipkart_search", "amazon_search", "flipkart_search_retry", "amazon_search_retry"):
        for result in stages.get(name) or []:
            entries.extend(result.get("candidates") or [])
    entries.extend(stages.get("generic_scrapes") or [])
    entries.extend((stages.get("retry") or {}).get("generic_scrapes") or [])
    return entries


def search_entries(trace: dict) -> dict:
    """site -> its search result dicts (both passes), for blocked/error notes."""
    stages = trace.get("stages") or {}
    out: dict = {}
    for name, site in (
        ("flipkart_search", "flipkart"), ("amazon_search", "amazon"),
        ("flipkart_search_retry", "flipkart"), ("amazon_search_retry", "amazon"),
    ):
        for entry in stages.get(name) or []:
            out.setdefault(site, []).append(entry)
    return out


def discovery_urls(trace: dict) -> list[str]:
    stages = trace.get("stages") or {}
    urls: list = []
    for result in stages.get("discovery") or []:
        urls.extend((result or {}).get("filtered_urls") or [])
    for result in ((stages.get("retry") or {}).get("discovery") or []):
        urls.extend((result or {}).get("filtered_urls") or [])
    return urls


def decisions_by_key(trace: dict) -> dict:
    """url_key -> verdict dict, first pass winning over the retry's copy."""
    stages = trace.get("stages") or {}
    decisions = list((stages.get("matching") or {}).get("all_decisions") or [])
    decisions.extend(((stages.get("retry") or {}).get("matching") or {}).get("all_decisions") or [])
    out: dict = {}
    for d in decisions:
        out.setdefault(url_key(d.get("candidate_url") or ""), d)
    return out


def skipped_keys(trace: dict) -> set:
    stages = trace.get("stages") or {}
    skipped = list((stages.get("matching") or {}).get("skipped") or [])
    skipped.extend(((stages.get("retry") or {}).get("matching") or {}).get("skipped") or [])
    return {url_key(u) for u in skipped if u}


def dropped_ranks(trace: dict) -> dict:
    """url_key -> {rank, score, title, top} for the listings the prefilter cut.

    rank counts how many listings of the same shop were ahead of it (kept or
    already-dropped, the dropped list runs in score order), so the listing was
    kept whenever N >= rank. That is the number the safe-N table sums."""
    prefilter = (trace.get("stages") or {}).get("prefilter") or {}
    top = prefilter.get("top") or 0
    ranks: dict = {}
    seen_per_shop: dict = {}
    for entry in prefilter.get("dropped") or []:
        shop = entry.get("source_domain") or entry.get("shop") or "?"
        index = seen_per_shop.get(shop, 0)
        seen_per_shop[shop] = index + 1
        key = url_key(entry.get("url") or "")
        ranks.setdefault(key, {
            "rank": top + index + 1,
            "score": entry.get("score"),
            "title": entry.get("title"),
            "top": top,
        })
    return ranks


def _fetch_error(entry: dict) -> str | None:
    if entry.get("fetch_error") and not entry.get("structured_fields") and not entry.get("raw_page_text"):
        return str(entry["fetch_error"])
    return None


def classify_slot(slot: dict, record: dict | None, trace: dict | None) -> tuple[str, str]:
    """(reason, detail) for one key slot whose URL is not in the results."""
    if record is None:
        return "no_results", "the product has no results line"
    if trace is None:
        return "no_trace", f"no trace file for {record.get('trace_id') or 'this run'}"
    wanted = slot["keys"]
    site = slot["site"]
    candidates = candidate_entries(trace)
    candidate_by_key = {url_key(c.get("url") or ""): c for c in candidates}
    decisions = decisions_by_key(trace)
    ranks = dropped_ranks(trace)
    skipped = skipped_keys(trace)

    for key in sorted(wanted):
        decision = decisions.get(key)
        if decision is None:
            continue
        reason = (decision.get("reason") or "").strip()
        if not decision.get("is_match"):
            if "[hard-rejected:" in reason:
                return "safety_gate", reason
            return "llm_said_no", f"LLM said no ({_pct(decision.get('confidence'))}): {reason[:160]}"
        confidence = decision.get("confidence")
        if confidence is None or confidence < MATCH_CONFIDENCE_THRESHOLD:
            return "below_threshold", (
                f"LLM said yes at {_pct(confidence)}, below the {_pct(MATCH_CONFIDENCE_THRESHOLD)} threshold"
            )
        if decision.get("price_used") is None:
            return "no_price_demoted", "LLM said yes but no price could be extracted"
        return "unexplained", "matched in the trace but missing from the results file"
    for key in sorted(wanted):
        if key in ranks:
            r = ranks[key]
            return "cut_by_filter", (
                f"cut by prefilter (score {r['score']}, shop rank {r['rank']} "
                f"with GEM_PREFILTER_TOP={r['top']})"
            )
    for key in sorted(wanted):
        entry = candidate_by_key.get(key)
        if entry is not None and _fetch_error(entry):
            return "fetch_failed", f"page fetch failed: {_fetch_error(entry)[:160]}"
    for key in sorted(wanted):
        if key in skipped:
            return "verify_incomplete", "the verdict never came back (LLM failure) — the listing was skipped"
    for key in sorted(wanted):
        if key in candidate_by_key:
            return "unexplained", "the listing reached the matcher but no verdict was recorded"

    details = []
    for entry in search_entries(trace).get(site) or []:
        if entry.get("blocked"):
            details.append(f"the {site} search was blocked")
        if entry.get("error"):
            details.append(f"the {site} search errored: {str(entry['error'])[:120]}")
    if any(url_key(u) in wanted for u in discovery_urls(trace)):
        details.append("discovery returned this URL but it was never fetched")
    return "never_found", "; ".join(details) or "no search returned this listing"


def _slot_found(slot: dict, matches: list) -> bool:
    for m in matches:
        url = m.get("url") or ""
        found_site = site_of(url)
        group = found_site if found_site in ("amazon", "flipkart") else "other"
        if group == slot["site"] and url_key(url) in slot["keys"]:
            return True
    return False


def analyze(rows: list[dict], results: dict[str, dict], traces_dir) -> dict:
    misses: list = []
    counts: dict = {}
    wanted = found = 0
    cut: list = []
    for row in rows:
        record = results.get(str(row.get("id", "")))
        matches = (record or {}).get("matches") or []
        for slot in key_slots(row):
            if slot["state"] != "urls":
                continue
            wanted += 1
            if _slot_found(slot, matches):
                found += 1
                continue
            trace = find_trace(traces_dir, (record or {}).get("trace_id"))
            reason, detail = classify_slot(slot, record, trace)
            counts[reason] = counts.get(reason, 0) + 1
            misses.append({
                "id": row.get("id"),
                "site": slot["site"],
                "urls": sorted(slot["keys"]),
                "reason": reason,
                "detail": detail,
            })
            if reason == "cut_by_filter" and trace is not None:
                ranks = dropped_ranks(trace)
                for key in slot["keys"]:
                    if key in ranks:
                        cut.append(ranks[key])
                        break
    safe_n = {"off": len(cut)}
    for n in SAFE_N_CHOICES:
        safe_n[str(n)] = sum(1 for r in cut if r["rank"] <= n)
    return {
        "slots": {"wanted": wanted, "found": found},
        "counts": counts,
        "misses": misses,
        "safe_n": safe_n,
    }


def render(out: dict) -> str:
    lines = []
    for m in out["misses"]:
        label = REASON_LABELS.get(m["reason"], m["reason"])
        lines.append(f"{m['id']}  {m['site']:<8}  {' | '.join(m['urls'])}  ->  {label} ({m['detail']})")
    wanted, found = out["slots"]["wanted"], out["slots"]["found"]
    lines.append("")
    lines.append(f"{found}/{wanted} checked slots found; {len(out['misses'])} missed")
    if out["counts"]:
        lines.append("Reasons: " + ", ".join(
            f"{REASON_LABELS.get(k, k)} {v}" for k, v in sorted(out["counts"].items())
        ))
    safe = out["safe_n"]
    if safe.get("off"):
        lines.append("")
        lines.append("Safe prefilter N (missed matches that would be put back in front of the LLM):")
        lines.append("  " + "  ".join(
            [f"off: {safe['off']}"] + [f"N={n}: {safe[str(n)]}" for n in SAFE_N_CHOICES]
        ))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--traces", default=DEFAULT_TRACES_DIR)
    ap.add_argument("--json", metavar="FILE", help="also write the full report here")
    args = ap.parse_args(argv)

    out = analyze(load_key(args.key), load_results(args.results), args.traces)
    print(render(out))
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nFull report written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
