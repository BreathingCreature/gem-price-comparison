"""Where the seconds and the LLM calls went, read from the traces.

    python tools/profile_runs.py --results eval/results/baseline.jsonl
    python tools/profile_runs.py --last 5
    python tools/profile_runs.py --trace 18d2ed8930fb471d

Every run writes a trace JSON with a stage marker before each step and one
llm_call event per call (provider, purpose, seconds). This tool only adds
those up: the wall time of each step, how many LLM calls happened inside it,
and, for all runs together, a table per provider and per purpose. LLM seconds
are exact even when steps run in parallel; step wall times overlap when they
do (GEM_PARALLEL=1), so their sum can exceed the run's total.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_TRACES_DIR = "traces"


def load_trace(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def newest_traces(traces_dir, count: int = 10) -> list:
    paths = sorted(Path(traces_dir).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[:count]


def traces_for_results(results_path, traces_dir) -> tuple[list, list]:
    """(trace files that exist, trace ids that have no file) of a results file."""
    found, missing = [], []
    for line in Path(results_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        trace_id = json.loads(line).get("trace_id")
        if not trace_id:
            continue
        path = Path(traces_dir) / f"{trace_id}.json"
        if path.exists():
            found.append(path)
        else:
            missing.append(str(trace_id))
    return found, missing


def profile_trace(trace: dict) -> dict:
    """One trace as numbers: total seconds, one row per step, one per
    (endpoint, provider, model, purpose) LLM group."""
    events = trace.get("events") or []
    stages = [e for e in events if e.get("type") == "stage"]
    llm_events = [e for e in events if e.get("type") == "llm_call"]
    cache_hit = any(e.get("type") == "cache_hit" for e in events)
    total = max((e.get("t") or 0.0) for e in events) if events else 0.0

    steps = []
    for i, stage in enumerate(stages):
        boundary = stages[i + 1]["seq"] if i + 1 < len(stages) else None
        window = [e for e in llm_events if e["seq"] > stage["seq"] and (boundary is None or e["seq"] < boundary)]
        end = stages[i + 1]["t"] if i + 1 < len(stages) else total
        steps.append({
            "name": stage.get("name") or "?",
            "label": stage.get("label") or "",
            "wall_seconds": round(end - stage["t"], 3),
            "llm_calls": len(window),
            "llm_seconds": round(sum(e.get("seconds") or 0.0 for e in window), 3),
        })

    grouped: dict = {}
    for e in llm_events:
        key = (e.get("endpoint"), e.get("provider"), e.get("model"), e.get("purpose") or "")
        row = grouped.setdefault(
            key,
            {"endpoint": key[0], "provider": key[1], "model": key[2], "purpose": key[3],
             "calls": 0, "failures": 0, "seconds": 0.0},
        )
        if e.get("ok"):
            row["calls"] += 1
        else:
            row["failures"] += 1
        row["seconds"] += e.get("seconds") or 0.0
    llm = sorted(grouped.values(), key=lambda r: (-r["seconds"], str(r["endpoint"])))
    for row in llm:
        row["seconds"] = round(row["seconds"], 3)

    return {
        "trace_id": trace.get("trace_id"),
        "gem_url": trace.get("gem_url"),
        "total_seconds": round(total, 3),
        "cache_hit": cache_hit,
        "steps": steps,
        "llm": llm,
    }


def aggregate(profiles: list) -> dict:
    """All profiles as one report: per-step totals plus per-provider and
    per-purpose LLM tables."""
    step_rows: dict = {}
    provider_rows: dict = {}
    purpose_rows: dict = {}
    for p in profiles:
        for s in p["steps"]:
            row = step_rows.setdefault(
                s["name"],
                {"name": s["name"], "runs": 0, "total_wall_seconds": 0.0, "llm_calls": 0, "llm_seconds": 0.0},
            )
            row["runs"] += 1
            row["total_wall_seconds"] += s["wall_seconds"]
            row["llm_calls"] += s["llm_calls"]
            row["llm_seconds"] += s["llm_seconds"]
        for e in p["llm"]:
            for table, key, field in ((provider_rows, e["provider"], "provider"), (purpose_rows, e["purpose"], "purpose")):
                row = table.setdefault(key, {field: key, "calls": 0, "failures": 0, "seconds": 0.0})
                row["calls"] += e["calls"]
                row["failures"] += e["failures"]
                row["seconds"] += e["seconds"]
    for row in step_rows.values():
        row["total_wall_seconds"] = round(row["total_wall_seconds"], 3)
        row["llm_seconds"] = round(row["llm_seconds"], 3)
        row["avg_wall_seconds"] = round(row["total_wall_seconds"] / row["runs"], 3)
    for table in (provider_rows, purpose_rows):
        for row in table.values():
            row["seconds"] = round(row["seconds"], 3)
    return {
        "runs": len(profiles),
        "total_seconds": round(sum(p["total_seconds"] for p in profiles), 3),
        "steps": sorted(step_rows.values(), key=lambda r: -r["total_wall_seconds"]),
        "providers": sorted(provider_rows.values(), key=lambda r: -r["seconds"]),
        "purposes": sorted(purpose_rows.values(), key=lambda r: -r["seconds"]),
    }


def render(profiles: list, agg: dict) -> str:
    lines = []
    for p in profiles:
        suffix = "  (cache hit)" if p["cache_hit"] else ""
        lines.append(f"Run {p['trace_id']}  total {p['total_seconds']}s{suffix}  {p.get('gem_url') or ''}")
        if p["steps"]:
            lines.append(f"  {'step':<26}{'wall s':>8}{'llm calls':>11}{'llm s':>8}")
            for s in p["steps"]:
                lines.append(f"  {s['name']:<26}{s['wall_seconds']:>8}{s['llm_calls']:>11}{s['llm_seconds']:>8}")
        lines.append("")
    lines.append(f"All runs: {agg['runs']} run(s), {agg['total_seconds']}s total")
    for s in agg["steps"]:
        lines.append(
            f"  {s['name']:<26} {s['runs']:>3} run(s)  wall {s['total_wall_seconds']:>9}s"
            f"  llm {s['llm_calls']:>4} calls / {s['llm_seconds']:>8}s"
        )
    if agg["providers"]:
        lines.append("LLM by provider:")
        for r in agg["providers"]:
            lines.append(f"  {str(r['provider']):<24} calls {r['calls']:>4}  failed {r['failures']:>3}  {r['seconds']:>9}s")
    if agg["purposes"]:
        lines.append("LLM by purpose:")
        for r in agg["purposes"]:
            lines.append(f"  {(r['purpose'] or '-'):<24} calls {r['calls']:>4}  failed {r['failures']:>3}  {r['seconds']:>9}s")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", metavar="JSONL", help="profile the traces named in these eval results")
    ap.add_argument("--trace", action="append", metavar="ID", help="a trace id or file path (repeatable)")
    ap.add_argument("--dir", default=DEFAULT_TRACES_DIR, help="traces directory")
    ap.add_argument("--last", type=int, default=10, help="with no --results/--trace: the newest N traces")
    args = ap.parse_args(argv)

    paths: list = []
    missing: list = []
    if args.trace:
        for t in args.trace:
            p = Path(t)
            if not p.exists():
                p = Path(args.dir) / f"{t}.json"
            (paths if p.exists() else missing).append(p if p.exists() else t)
    elif args.results:
        paths, missing = traces_for_results(args.results, args.dir)
    else:
        paths = newest_traces(args.dir, args.last)

    if not paths:
        print(f"no traces found (looked in {args.dir})")
        return 1
    profiles = [profile_trace(load_trace(p)) for p in paths]
    print(render(profiles, aggregate(profiles)))
    if missing:
        print(f"\n{len(missing)} trace id(s) have no file: {', '.join(str(m) for m in missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
