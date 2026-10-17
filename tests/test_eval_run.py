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
