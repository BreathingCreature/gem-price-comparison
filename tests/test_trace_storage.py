"""Trace files on disk: compact JSON, and a rotation scan that runs every
N-th write instead of on every single run (E7)."""
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from core import pipeline


@contextmanager
def _patched(obj, **attrs):
    """Temporary attribute swaps - works with and without pytest's fixture."""
    old = {name: getattr(obj, name) for name in attrs}
    for name, value in attrs.items():
        setattr(obj, name, value)
    try:
        yield
    finally:
        for name, value in old.items():
            setattr(obj, name, value)


def test_a_trace_is_written_as_one_compact_line():
    with tempfile.TemporaryDirectory() as tmp:
        with _patched(pipeline, TRACES_DIR=Path(tmp)):
            pipeline._write_trace({"trace_id": "abc123", "events": [{"type": "x"}]})
        text = (Path(tmp) / "abc123.json").read_text(encoding="utf-8")
        assert "\n" not in text  # compact JSON: no indent=2 air in 100-200KB files
        assert json.loads(text) == {"trace_id": "abc123", "events": [{"type": "x"}]}


def test_the_rotation_scan_runs_every_nth_write_only():
    with tempfile.TemporaryDirectory() as tmp:
        traces = Path(tmp)
        now = time.time()
        for i, age in enumerate((300, 200, 100)):
            old = traces / f"old{i}.json"
            old.write_text("{}", encoding="utf-8")
            os.utime(old, (now - age, now - age))
        with _patched(pipeline, TRACES_DIR=traces, TRACE_MAX_FILES=2, TRACE_ROTATE_EVERY=3,
                      _trace_writes_since_rotation=0):
            assert len(list(traces.glob("*.json"))) == 3  # already over the cap
            pipeline._write_trace({"trace_id": "new1"})
            pipeline._write_trace({"trace_id": "new2"})
            assert len(list(traces.glob("*.json"))) == 5  # no scan yet
            pipeline._write_trace({"trace_id": "new3"})  # third write -> scan
            kept = sorted(p.name for p in traces.glob("*.json"))
        # The three old files are gone. Which two of the fresh writes
        # survive depends on the filesystem's mtime tick (writes this close
        # can share one), so only count and freshness are asserted.
        assert len(kept) == 2 and all(name.startswith("new") for name in kept)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
