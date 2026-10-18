"""Kit 20: where the seconds and the LLM calls went (tools/profile_runs.py).

The trace JSON already carries everything: stage markers with timestamps and
one llm_call event per call with its provider, purpose and seconds. This tool
only adds them up, per step and per provider.
"""
import json

from tools import profile_runs


def _trace(trace_id="t1", gem_url="https://mkp.gem.gov.in/p-1"):
    return {
        "trace_id": trace_id,
        "gem_url": gem_url,
        "stages": {},
        "events": [
            {"seq": 1, "t": 0.0, "type": "run_started"},
            {"seq": 2, "t": 0.5, "type": "stage", "name": "extract", "label": "Reading the GeM product page"},
            {"seq": 3, "t": 1.2, "type": "stage", "name": "normalize", "label": "Working out what the product really is"},
            {"seq": 4, "t": 2.0, "type": "llm_call", "endpoint": "ep1", "provider": "nvidia",
             "model": "nemotron", "purpose": "normalize", "seconds": 0.8, "ok": True},
            {"seq": 5, "t": 3.6, "type": "stage", "name": "search_flipkart", "label": "Searching Flipkart"},
            {"seq": 6, "t": 9.8, "type": "stage", "name": "match", "label": "Checking which listings are the same product"},
            {"seq": 7, "t": 11.0, "type": "llm_call", "endpoint": "ep2", "provider": "openrouter",
             "model": "qwen", "purpose": "verify", "seconds": 1.2, "ok": True},
            {"seq": 8, "t": 11.4, "type": "llm_call", "endpoint": "ep2", "provider": "openrouter",
             "model": "qwen", "purpose": "verify", "seconds": 0.4, "ok": False},
            {"seq": 9, "t": 12.4, "type": "run_finished"},
        ],
    }


def test_steps_get_the_wall_time_between_the_stage_markers():
    profile = profile_runs.profile_trace(_trace())
    assert profile["total_seconds"] == 12.4
    steps = {s["name"]: s for s in profile["steps"]}
    assert steps["extract"]["wall_seconds"] == 0.7
    assert steps["normalize"]["wall_seconds"] == 2.4  # includes its llm call
    assert steps["normalize"]["llm_calls"] == 1 and steps["normalize"]["llm_seconds"] == 0.8
    assert steps["search_flipkart"]["wall_seconds"] == 6.2 and steps["search_flipkart"]["llm_calls"] == 0
    assert steps["match"]["llm_calls"] == 2 and steps["match"]["llm_seconds"] == 1.6


def test_llm_calls_are_grouped_by_provider_and_purpose():
    profile = profile_runs.profile_trace(_trace())
    llm = {(row["provider"], row["purpose"]): row for row in profile["llm"]}
    assert llm[("openrouter", "verify")]["calls"] == 1
    assert llm[("openrouter", "verify")]["failures"] == 1
    assert llm[("openrouter", "verify")]["seconds"] == 1.6
    assert llm[("nvidia", "normalize")]["seconds"] == 0.8


def test_two_runs_are_summed_in_the_aggregate():
    profiles = [profile_runs.profile_trace(_trace()), profile_runs.profile_trace(_trace("t2"))]
    agg = profile_runs.aggregate(profiles)
    assert agg["runs"] == 2
    assert agg["total_seconds"] == 24.8
    providers = {row["provider"]: row for row in agg["providers"]}
    assert providers["openrouter"]["calls"] == 2 and providers["openrouter"]["failures"] == 2
    purposes = {row["purpose"]: row for row in agg["purposes"]}
    assert purposes["verify"]["seconds"] == 3.2
    steps = {s["name"]: s for s in agg["steps"]}
    assert steps["match"]["llm_calls"] == 4 and steps["match"]["total_wall_seconds"] == 5.2


def test_a_cache_hit_run_is_shown_as_such():
    trace = {
        "trace_id": "t9",
        "gem_url": "u",
        "events": [
            {"seq": 1, "t": 0.0, "type": "run_started"},
            {"seq": 2, "t": 0.01, "type": "cache_hit"},
            {"seq": 3, "t": 0.01, "type": "run_finished"},
        ],
    }
    profile = profile_runs.profile_trace(trace)
    assert profile["cache_hit"] is True and profile["steps"] == []
    assert profile["total_seconds"] == 0.01


def test_traces_for_results_reads_the_trace_ids(tmp_path):
    results = tmp_path / "r.jsonl"
    results.write_text(
        "\n".join([
            json.dumps({"id": "P1", "trace_id": "t1"}),
            json.dumps({"id": "P2", "trace_id": "t2"}),
            json.dumps({"id": "P3", "trace_id": "gone"}),
        ]),
        encoding="utf-8",
    )
    traces_dir = tmp_path / "traces"  # conftest already created it for this test
    for tid in ("t1", "t2"):
        (traces_dir / f"{tid}.json").write_text("{}", encoding="utf-8")
    found, missing = profile_runs.traces_for_results(results, traces_dir)
    assert [p.name for p in found] == ["t1.json", "t2.json"]
    assert missing == ["gone"]


def test_render_shows_steps_and_providers():
    profiles = [profile_runs.profile_trace(_trace())]
    text = profile_runs.render(profiles, profile_runs.aggregate(profiles))
    for needle in ("search_flipkart", "openrouter", "verify", "12.4"):
        assert needle in text


def test_cli_main_with_a_results_file(tmp_path, capsys):
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps({"id": "P1", "trace_id": "t1"}) + "\n", encoding="utf-8")
    traces_dir = tmp_path / "traces"
    (traces_dir / "t1.json").write_text(json.dumps(_trace()), encoding="utf-8")
    rc = profile_runs.main(["--results", str(results), "--dir", str(traces_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "openrouter" in out and "search_flipkart" in out


def test_cli_main_with_no_traces_says_so(tmp_path, capsys):
    empty = tmp_path / "traces"  # conftest created it, it stays empty
    rc = profile_runs.main(["--dir", str(empty)])
    assert rc == 1
    assert "no traces" in capsys.readouterr().out.lower()
