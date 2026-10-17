"""The evaluation runner: one record per product, resumable, never stops on one failure."""
import json

from eval import run_eval

ROWS = [
    {"id": "P1", "gem_url": "https://mkp.gem.gov.in/a"},
    {"id": "P2", "gem_url": "https://mkp.gem.gov.in/b"},
    {"id": "P3", "gem_url": "https://mkp.gem.gov.in/c"},
]


def _fake_run(gem_url, refresh=False, on_event=None):
    assert refresh is True  # every evaluation run must be a fresh one
    if gem_url.endswith("/b"):
        raise RuntimeError("shop blocked us")
    on_event({"type": "llm_call", "ok": True, "model": "m1"})
    on_event({"type": "llm_call", "ok": False, "model": "m1"})
    on_event({"type": "llm_cache_hit"})
    return {
        "trace_id": "t-" + gem_url[-1],
        "matches": [{"candidate_url": "https://www.amazon.in/dp/B0ABCDEFGH", "source_domain": "amazon.in",
                     "price_used": 99.0, "confidence": 0.9, "reason": "dropped"}],
        "not_found_on": ["flipkart.com"],
        "search_issues": [],
    }


def test_each_product_gets_one_line_and_a_failure_does_not_stop_the_batch(tmp_path):
    out = tmp_path / "r.jsonl"
    assert run_eval.run_all(ROWS, out, run_fn=_fake_run, log=lambda *_: None) == 3
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert [r["id"] for r in lines] == ["P1", "P2", "P3"]
    ok, bad = lines[0], lines[1]
    assert ok["matches"] == [{"url": "https://www.amazon.in/dp/B0ABCDEFGH", "domain": "amazon.in", "price": 99.0, "confidence": 0.9}]
    assert (ok["llm_calls"], ok["llm_failures"], ok["cache_hits"], ok["llm_models"]) == (1, 1, 1, {"m1": 1})
    assert ok["not_found_on"] == ["flipkart.com"] and ok["error"] is None and ok["seconds"] >= 0
    assert "shop blocked us" in bad["error"] and bad["matches"] == []


def test_a_second_run_only_does_what_is_missing(tmp_path):
    out = tmp_path / "r.jsonl"
    run_eval.run_all(ROWS, out, run_fn=_fake_run, limit=2, log=lambda *_: None)
    assert len(out.read_text().splitlines()) == 2
    # P2 errored on the first run, so it must be retried on resume; only P1 is done.
    assert run_eval.run_all(ROWS, out, run_fn=_fake_run, log=lambda *_: None) == 2
    assert len(out.read_text().splitlines()) == 4


def test_only_filter(tmp_path):
    out = tmp_path / "r.jsonl"
    run_eval.run_all(ROWS, out, run_fn=_fake_run, only={"P3"}, log=lambda *_: None)
    assert [json.loads(x)["id"] for x in out.read_text().splitlines()] == ["P3"]


def test_settings_snapshot_lists_only_what_is_set(monkeypatch):
    monkeypatch.delenv("GEM_PARALLEL", raising=False)
    monkeypatch.setenv("GEM_RETRY", "0")
    snap = run_eval.settings_snapshot()
    assert snap["settings"].get("GEM_RETRY") == "0" and "GEM_PARALLEL" not in snap["settings"]


def test_settings_snapshot_loads_dotenv(monkeypatch):
    called = []
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: called.append(True))
    monkeypatch.setenv("GEM_PARALLEL", "1")
    snap = run_eval.settings_snapshot()
    assert called  # load_dotenv was invoked so .env values are visible
    assert snap["settings"]["GEM_PARALLEL"] == "1"


def test_settings_snapshot_records_active_providers(monkeypatch):
    class _FakeEndpoint:
        def __init__(self, name, model, enabled):
            self.name = name
            self.model = model
            self.enabled = enabled

    monkeypatch.setattr("llm.router.load_endpoints", lambda: ([_FakeEndpoint("nvidia", "m1", True)], None))
    monkeypatch.setenv("GEM_PARALLEL", "1")
    snap = run_eval.settings_snapshot()
    assert snap["settings"]["llm_providers"] == [{"name": "nvidia", "model": "m1", "enabled": True}]


def test_all_llm_failures_with_no_verdicts_marked_error(tmp_path):
    out = tmp_path / "r.jsonl"

    def _fake_run_all_fail(gem_url, refresh=False, on_event=None):
        on_event({"type": "llm_call", "ok": False, "model": "m1"})
        on_event({"type": "llm_call", "ok": False, "model": "m1"})
        on_event({"type": "llm_call", "ok": False, "model": "m1"})
        return {"trace_id": "t", "matches": [], "not_found_on": [], "search_issues": []}

    run_eval.run_all([{"id": "P1", "gem_url": "https://mkp.gem.gov.in/a"}], out, run_fn=_fake_run_all_fail, log=lambda *_: None)
    rec = __import__("json").loads(out.read_text().splitlines()[0])
    assert rec["error"] is not None
    assert "LLM call" in rec["error"]


# --- A1: error condition is "LLM never answered" -------------------------

def _fake_run_pattern(failures, ok_calls, cache_hits, matches, gem_url, refresh=False, on_event=None):
    for _ in range(failures):
        on_event({"type": "llm_call", "ok": False, "model": "m1"})
    for _ in range(ok_calls):
        on_event({"type": "llm_call", "ok": True, "model": "m1"})
    for _ in range(cache_hits):
        on_event({"type": "llm_cache_hit"})
    return {"trace_id": "t", "matches": matches, "not_found_on": [], "search_issues": []}


def _fake_run_pattern(failures, ok_calls, cache_hits, matches, gem_url, refresh=False, on_event=None):
    for _ in range(failures):
        on_event({"type": "llm_call", "ok": False, "model": "m1"})
    for _ in range(ok_calls):
        on_event({"type": "llm_call", "ok": True, "model": "m1"})
    for _ in range(cache_hits):
        on_event({"type": "llm_cache_hit"})
    return {"trace_id": "t", "matches": matches, "not_found_on": [], "search_issues": []}


def test_a1_failures_plus_ok_calls_is_not_error(tmp_path):
    out = tmp_path / "r.jsonl"
    run_eval.run_all([{"id": "P1", "gem_url": "https://mkp.gem.gov.in/a"}], out,
                     run_fn=lambda u, refresh=False, on_event=None: _fake_run_pattern(3, 5, 0,
                         [{"candidate_url": "https://www.amazon.in/dp/B0X", "source_domain": "amazon.in",
                           "price_used": 99.0, "confidence": 0.9}], u, refresh, on_event),
                     log=lambda *_: None)
    rec = __import__("json").loads(out.read_text().splitlines()[0])
    assert rec["error"] is None


def test_a1_all_failures_no_calls_is_error(tmp_path):
    out = tmp_path / "r.jsonl"
    run_eval.run_all([{"id": "P1", "gem_url": "https://mkp.gem.gov.in/a"}], out,
                     run_fn=lambda u, refresh=False, on_event=None: _fake_run_pattern(3, 0, 0, [], u, refresh, on_event),
                     log=lambda *_: None)
    rec = __import__("json").loads(out.read_text().splitlines()[0])
    assert rec["error"] is not None
    assert "LLM call" in rec["error"]


def test_a1_cache_hits_only_is_not_error(tmp_path):
    out = tmp_path / "r.jsonl"
    run_eval.run_all([{"id": "P1", "gem_url": "https://mkp.gem.gov.in/a"}], out,
                     run_fn=lambda u, refresh=False, on_event=None: _fake_run_pattern(0, 0, 3, [], u, refresh, on_event),
                     log=lambda *_: None)
    rec = __import__("json").loads(out.read_text().splitlines()[0])
    assert rec["error"] is None


# --- A4: meta settings comparison on label reuse -------------------------

def test_a4_meta_settings_changed_warns_and_keeps_old(tmp_path, monkeypatch, capsys):
    import csv
    key_file = tmp_path / "key.csv"
    key_file.write_text("id,gem_url\nP1,https://mkp.gem.gov.in/a\n")
    
    meta = tmp_path / "before.meta.json"
    
    # First run with GEM_PARALLEL=0 — write meta as if a previous run happened
    monkeypatch.setenv("GEM_PARALLEL", "0")
    monkeypatch.setenv("GEM_RETRY", "1")
    snap = run_eval.settings_snapshot()
    meta.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    
    # Change settings and mock run_all to do nothing; main() will compare settings
    monkeypatch.setenv("GEM_PARALLEL", "1")
    monkeypatch.setattr(run_eval, "run_all", lambda rows, out_path, **k: None)
    
    run_eval.main(["--key", str(key_file), "--label", "before", "--dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert "settings changed" in captured.out
    assert "GEM_PARALLEL" in captured.out
    assert "Use a new --label" in captured.out
    
    # Meta not overwritten
    old = json.loads(meta.read_text(encoding="utf-8"))
    assert old["settings"]["GEM_PARALLEL"] == "0"


def test_a4_meta_settings_same_no_warning(tmp_path, monkeypatch, capsys):
    import csv
    key_file = tmp_path / "key.csv"
    key_file.write_text("id,gem_url\nP1,https://mkp.gem.gov.in/a\n")
    
    meta = tmp_path / "before.meta.json"
    
    monkeypatch.setenv("GEM_PARALLEL", "0")
    monkeypatch.setenv("GEM_RETRY", "1")
    snap = run_eval.settings_snapshot()
    meta.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    
    monkeypatch.setenv("GEM_PARALLEL", "0")
    monkeypatch.setenv("GEM_RETRY", "1")
    monkeypatch.setattr(run_eval, "run_all", lambda rows, out_path, **k: None)
    
    run_eval.main(["--key", str(key_file), "--label", "before", "--dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert "settings changed" not in captured.out
