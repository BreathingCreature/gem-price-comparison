"""Run the pipeline over every product of the answer key and save the results.

    python -m eval.run_eval --key eval/answer_key.csv --label before
    python -m eval.run_eval --key eval/answer_key.csv --label before --limit 3
    python -m eval.run_eval --key eval/answer_key.csv --label before --only P01,P07
    python -m eval.run_eval --key eval/answer_key.csv --label before --workers 2

With --workers 2 a couple of products run at the same time (most of a run is
waiting on Chrome and the shops, so 2-3 helps and 10 does not). Records are
still one line each; with workers they land in completion order.
Writes eval/results/<label>.jsonl (one line per product) and
eval/results/<label>.meta.json (the settings and the git commit used).
Safe to stop and start again: products already in the file are skipped.
Every run is a fresh one (the one-hour result cache is bypassed).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from eval.score import load_key

SETTING_NAMES = [
    "GEM_PARALLEL", "GEM_RETRY", "GEM_GATES", "LLM_CACHE", "PRICE_SANITY_LOW_RATIO",
    "PRICE_SANITY_HIGH_RATIO", "MATCH_CONFIDENCE_THRESHOLD", "LLM_PROVIDERS_FILE",
    "GEM_LLM_WORKERS", "GEM_PREFILTER_TOP", "GEM_PRICE_PREGATE", "GEM_REUSE_VERDICTS",
    "GEM_DISCOVERY", "GEM_FLIPKART_LIGHT", "GEM_DISCOVERY_TIME_LIMIT",
]


def _default_run(gem_url, **kwargs):
    from core.pipeline import run_pipeline

    return run_pipeline(gem_url, **kwargs)


def record_for(row: dict, run_fn) -> dict:
    """Run one product and describe what happened."""
    models: Counter = Counter()
    counts = {"llm_calls": 0, "llm_failures": 0, "cache_hits": 0}

    def on_event(e):
        if e.get("type") == "llm_call":
            if e.get("ok"):
                counts["llm_calls"] += 1
                models[e.get("model") or "?"] += 1
            else:
                counts["llm_failures"] += 1
        elif e.get("type") == "llm_cache_hit":
            counts["cache_hits"] += 1

    started = time.perf_counter()
    error, result = None, {}
    try:
        result = run_fn(row["gem_url"], refresh=True, on_event=on_event) or {}
    except Exception as e:  # one broken product must not stop the whole batch
        error = f"{type(e).__name__}: {e}"
    if error is None and counts["llm_failures"] > 0 and counts["llm_calls"] == 0 and counts["cache_hits"] == 0:
        error = "all LLM call(s) failed, no match verdicts"
    seconds = round(time.perf_counter() - started, 2)
    return {
        "id": row["id"],
        "gem_url": row["gem_url"],
        "seconds": seconds,
        "error": error,
        "trace_id": result.get("trace_id"),
        "matches": [
            {
                "url": m.get("candidate_url"),
                "domain": m.get("source_domain"),
                "price": m.get("price_used"),
                "confidence": m.get("confidence"),
            }
            for m in (result.get("matches") or [])
        ],
        "not_found_on": result.get("not_found_on") or [],
        "search_issues": result.get("search_issues") or [],
        **counts,
        "llm_models": dict(models),
    }


def _append_record(out_path: Path, rec: dict, n: int, total: int, log) -> None:
    """Append one finished product to the jsonl and say what happened.

    Called from the main thread only, so parallel workers never interleave
    a line."""
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    state = "ERROR " + rec["error"][:80] if rec["error"] else f"{len(rec['matches'])} match(es)"
    log(f"[{n}/{total}] {rec['id']}: {state} in {rec['seconds']}s")


def run_all(rows: list[dict], out_path: Path, *, run_fn=None, limit=None, only=None, workers: int = 1, log=print) -> int:
    run_fn = run_fn or _default_run
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                # A row that errored must be retried on resume — only clean
                # rows are "done".
                if not rec.get("error"):
                    done.add(str(rec["id"]))
    todo = [r for r in rows if r.get("gem_url") and r["id"] not in done and (not only or r["id"] in only)]
    if limit:
        todo = todo[:limit]
    log(f"{len(done)} already done, {len(todo)} to run")
    if workers <= 1 or len(todo) <= 1:
        for n, row in enumerate(todo, start=1):
            _append_record(out_path, record_for(row, run_fn), n, len(todo), log)
        return len(todo)
    # B9: each product is mostly waiting on Chrome and the shops, so a couple
    # at a time cuts a whole key's wall time roughly in half.
    log(f"running {workers} products at a time")
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="eval-product") as pool:
        futures = {pool.submit(record_for, row, run_fn): row for row in todo}
        for n, future in enumerate(as_completed(futures), start=1):
            _append_record(out_path, future.result(), n, len(todo), log)
    return len(todo)


def settings_snapshot() -> dict:
    # Load .env so values only set there (not in the shell) are recorded.
    from dotenv import load_dotenv

    load_dotenv()
    snap = {name: os.getenv(name) for name in SETTING_NAMES if os.getenv(name) is not None}
    # Record which LLM providers/models were active for this run's meta.
    try:
        from llm.router import load_endpoints

        endpoints, _ = load_endpoints()
        snap["llm_providers"] = [
            {"name": e.name, "model": e.model, "enabled": e.enabled} for e in endpoints
        ]
    except Exception:
        snap["llm_providers"] = []
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        commit = ""
    return {"settings": snap, "git_commit": commit or None, "started": datetime.now().isoformat(timespec="seconds")}


def write_meta(meta: Path, label: str, *, extra: dict | None = None, log=print) -> None:
    """Write <label>.meta.json, or warn (and keep the old file) when the label
    already ran with different settings. Shared with eval/replay.py, which adds
    its own "replay" section through extra."""
    snapshot = settings_snapshot()
    if extra:
        snapshot.update(extra)
    if meta.exists():
        old_meta = json.loads(meta.read_text(encoding="utf-8"))
        old_settings = old_meta.get("settings", {})
        new_settings = snapshot.get("settings", {})
        diff = {k: (old_settings.get(k), new_settings.get(k))
                for k in set(old_settings) | set(new_settings)
                if old_settings.get(k) != new_settings.get(k)}
        if diff:
            log(f"Warning: settings changed since last run with label '{label}':")
            for k in sorted(diff):
                old_v, new_v = diff[k]
                log(f"  {k}: {old_v!r} -> {new_v!r}")
            log("Use a new --label for a new setting.")
    else:
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True)
    ap.add_argument("--label", required=True, help="a short name for this run, e.g. before or parallel-on")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="comma-separated product ids")
    ap.add_argument("--workers", type=int, default=1,
                    help="products to evaluate at the same time (2-3 helps; Chrome and the shops are the limit)")
    ap.add_argument("--dir", default="eval/results")
    args = ap.parse_args(argv)

    out = Path(args.dir) / f"{args.label}.jsonl"
    meta = Path(args.dir) / f"{args.label}.meta.json"
    rows = load_key(args.key)
    write_meta(meta, args.label)
    only = set(args.only.split(",")) if args.only else None
    run_all(rows, out, limit=args.limit, only=only, workers=args.workers)
    print(f"results: {out}\nscore them with: python -m eval.score --key {args.key} --results {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
