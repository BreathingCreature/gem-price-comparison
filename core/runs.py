"""Small on-disk index of finished runs.

Every run already writes a trace, but a trace is keyed by an id nobody knows
after a restart. This store keeps one small JSON per run under
cache/runs/<run_id>.json with the few fields a "recent runs" list needs
(title, when, state, cheapest price, match count), so the frontend can show
them without scanning traces. list_runs() reads them newest first.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core import config
from core.errors import redact_obj

RUNS_DIR_NAME = "runs"
SUMMARY_FIELDS = (
    "run_id",
    "gem_url",
    "title",
    "state",
    "checked_at",
    "cheapest_price",
    "match_count",
    "from_cache",
)


def runs_dir() -> Path:
    """cache/runs — resolved per call so tests can point CACHE_DIR elsewhere."""
    return config.CACHE_DIR / RUNS_DIR_NAME


def save_run(record: dict) -> Path:
    """Write (or rewrite) one run record atomically. Returns its path."""
    record = dict(record)
    run_id = str(record.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("a run record needs a run_id")
    record.setdefault("saved_at", time.time())
    directory = runs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(redact_obj(record), ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def list_runs(limit: int = 20) -> list[dict]:
    """The newest runs first. A damaged record is skipped, never fatal."""
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 20
    directory = runs_dir()
    if not directory.exists():
        return []
    records = []
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    records.sort(key=lambda r: r.get("saved_at") or 0, reverse=True)
    return records[:limit]


def summarize(record: dict) -> dict:
    """The fields the recent-runs list shows."""
    return {key: record.get(key) for key in SUMMARY_FIELDS if key in record}
