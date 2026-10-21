"""The HTTP layer: start a run, stream its events, read the result."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from core import events, pipeline
from llm import router as router_module
from llm.router import Endpoint, Router

GEM_URL = "https://mkp.gem.gov.in/computer-mouse/demo-mouse/p-500-600-cat.html"


def fake_runner(gem_url, refresh, on_event):
    with events.capture(on_event):
        events.emit("run_started", gem_url=gem_url, refresh=refresh)
        events.emit("stage", name="extract", label="Reading")
        events.emit("run_finished", from_cache=False, matches=0, cheapest=None, savings=None, trace_id=None)
    return {"matches": [], "from_cache": False}


def failing_runner(gem_url, refresh, on_event):
    raise RuntimeError("the GeM page could not be read")


@pytest.fixture
def client():
    return TestClient(create_app(runner=fake_runner, demo=False))


def wait_until_finished(client, run_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("done", "failed"):
            return run
        time.sleep(0.02)
    raise AssertionError("the run never finished")


def read_stream(client, run_id, query=""):
    """Returns (events, done_payload) from the server-sent-events stream."""
    seen, done = [], None
    pending_event = None
    with client.stream("GET", f"/api/runs/{run_id}/events{query}") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("event:"):
                pending_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1])
                if pending_event == "done":
                    done = payload
                else:
                    seen.append(payload)
                pending_event = None
    return seen, done


def test_health_says_whether_it_is_demo_mode(client):
    assert client.get("/api/health").json() == {"ok": True, "demo": False}


def test_a_link_that_is_not_gem_is_refused(client):
    response = client.post("/api/runs", json={"gem_url": "https://example.com/not-gem"})
    assert response.status_code == 422
    assert "GeM" in response.json()["detail"]


def test_starting_a_run_returns_an_id_and_the_run_completes(client):
    response = client.post("/api/runs", json={"gem_url": GEM_URL})
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    run = wait_until_finished(client, run_id)
    assert run["status"] == "done"
    assert run["result"] == {"matches": [], "from_cache": False}
    assert run["gem_url"] == GEM_URL and run["event_count"] == 3


def test_the_refresh_flag_reaches_the_runner():
    received = {}

    def spy(gem_url, refresh, on_event):
        received["refresh"] = refresh
        return {}

    client = TestClient(create_app(runner=spy, demo=False))
    run_id = client.post("/api/runs", json={"gem_url": GEM_URL, "refresh": True}).json()["run_id"]
    wait_until_finished(client, run_id)
    assert received["refresh"] is True


def test_the_event_stream_replays_everything_then_says_done(client):
    run_id = client.post("/api/runs", json={"gem_url": GEM_URL}).json()["run_id"]
    wait_until_finished(client, run_id)

    seen, done = read_stream(client, run_id)
    assert [e["type"] for e in seen] == ["run_started", "stage", "run_finished"]
    assert [e["seq"] for e in seen] == [1, 2, 3]
    assert done == {"status": "done"}


def test_the_stream_can_continue_after_an_event_number(client):
    run_id = client.post("/api/runs", json={"gem_url": GEM_URL}).json()["run_id"]
    wait_until_finished(client, run_id)

    seen, _ = read_stream(client, run_id, query="?after=2")
    assert [e["type"] for e in seen] == ["run_finished"]


def test_a_failing_run_reports_run_failed(client):
    client = TestClient(create_app(runner=failing_runner, demo=False))
    run_id = client.post("/api/runs", json={"gem_url": GEM_URL}).json()["run_id"]

    run = wait_until_finished(client, run_id)
    assert run["status"] == "failed"
    assert "could not be read" in run["error"]
    seen, done = read_stream(client, run_id)
    assert seen[-1]["type"] == "run_failed" and "could not be read" in seen[-1]["error"]
    assert done == {"status": "failed"}


def test_unknown_run_is_a_404(client):
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/events").status_code == 404


def test_recent_runs_are_listed_newest_first(client):
    first = client.post("/api/runs", json={"gem_url": GEM_URL}).json()["run_id"]
    second = client.post("/api/runs", json={"gem_url": GEM_URL}).json()["run_id"]

    ids = [r["id"] for r in client.get("/api/runs").json()["runs"]]
    assert ids[:2] == [second, first]


def test_a_saved_trace_can_be_fetched(client):
    (pipeline.TRACES_DIR / "abcdef0123456789.json").write_text('{"hello": "trace"}', encoding="utf-8")
    assert client.get("/api/traces/abcdef0123456789").json() == {"hello": "trace"}


def test_trace_ids_cannot_escape_the_traces_folder(client):
    assert client.get("/api/traces/..%2F..%2Fsecret").status_code == 404
    assert client.get("/api/traces/not-hex-at-all").status_code == 404
    assert client.get("/api/traces/abcdef0123456789").status_code == 404  # valid shape, no such file


def test_a_damaged_trace_is_a_500_not_an_unhandled_crash(client):
    (pipeline.TRACES_DIR / "abcdef0123456789.json").write_text("{not json", encoding="utf-8")
    response = client.get("/api/traces/abcdef0123456789")
    assert response.status_code == 500
    assert "damaged" in response.json()["detail"]


def test_a_gem_link_is_stripped_before_validation():
    client = TestClient(create_app(runner=fake_runner, demo=False))
    response = client.post("/api/runs", json={"gem_url": f"  {GEM_URL}  "})
    assert response.status_code == 202
    run = wait_until_finished(client, response.json()["run_id"])
    assert run["gem_url"] == GEM_URL


def test_a_finished_live_run_is_not_listed_twice():
    from core import runs as runs_store

    run_id = "abc123abc123abcd"

    def saved_runner(gem_url, refresh, on_event):
        with events.capture(on_event):
            events.emit("run_started", gem_url=gem_url, refresh=refresh)
        return {"matches": [], "trace_id": run_id}

    client = TestClient(create_app(runner=saved_runner, demo=False))
    job_id = client.post("/api/runs", json={"gem_url": GEM_URL}).json()["run_id"]
    wait_until_finished(client, job_id)
    runs_store.save_run({"run_id": run_id, "gem_url": GEM_URL, "saved_at": 1, "state": "done"})

    entries = [r for r in client.get("/api/runs").json()["runs"] if r["gem_url"] == GEM_URL]
    assert len(entries) == 1
    assert entries[0]["id"] == job_id and entries[0]["live"] is True


def test_llm_status_lists_every_endpoint(client, monkeypatch):
    endpoints = [
        Endpoint(provider="one", model="m", base_url="https://one.test/v1", api_key_env="K"),
        Endpoint(provider="two", model="m", base_url="https://two.test/v1", api_key_env="K", enabled=False, disabled_reason="K is not set"),
    ]
    monkeypatch.setattr(router_module, "_router", Router(endpoints, strategy="spread"))

    body = client.get("/api/llm/status").json()
    assert body["strategy"] == "spread"
    assert [(e["endpoint"], e["state"]) for e in body["endpoints"]] == [("one/m", "ready"), ("two/m", "disabled")]


def test_the_real_app_warms_up_once_at_startup(monkeypatch):
    calls = []
    monkeypatch.setattr("api.server._warm_up", lambda: calls.append(1))
    with TestClient(create_app(demo=False)) as client:
        assert client.get("/api/health").json()["ok"] is True
    assert calls == [1]


def test_demo_and_injected_runners_never_warm_up(monkeypatch):
    calls = []
    monkeypatch.setattr("api.server._warm_up", lambda: calls.append(1))
    with TestClient(create_app(demo=True)):
        pass
    with TestClient(create_app(runner=fake_runner, demo=False)):
        pass
    assert calls == []


def test_demo_mode_runs_a_fake_pipeline_and_accepts_any_link(monkeypatch):
    from api import demo

    monkeypatch.setattr(demo, "DEMO_DELAY", 0)
    client = TestClient(create_app(demo=True))
    assert client.get("/api/health").json()["demo"] is True

    run_id = client.post("/api/runs", json={"gem_url": "https://example.com/anything"}).json()["run_id"]
    run = wait_until_finished(client, run_id)

    assert run["status"] == "done"
    result = run["result"]
    assert result["state"] == "done"
    assert [(m["shop"], m["price"]) for m in result["matches"]] == [("flipkart.com", 1149.0), ("amazon.in", 1199.0)]
    assert result["cheapest"]["shop"] == "flipkart.com"
    assert result["cheapest"]["saving_vs_gem"] == 150.0
    assert result["shops"]["amazon.in"]["status"] == "found"
    assert result["shops"]["flipkart.com"]["status"] == "found" and result["shops"]["flipkart.com"]["retried"] is True
    assert result["shops"]["example-shop.in"]["status"] == "not_found"
    seen, _ = read_stream(client, run_id)
    types = {e["type"] for e in seen}
    assert {"gem_product", "search_finished", "candidate_verdict", "run_finished", "shop_started",
            "shop_finished", "retry_started", "shop_updated", "run_updated", "done"} <= types
    assert [e["type"] for e in seen][-1] == "done"
    # A demo run is fake data, so it must never appear in "recent runs".
    from core import runs as runs_store

    assert runs_store.list_runs() == []


# --- B9: GEM_JOBS lets several runs share the server ----------------------

def test_gem_jobs_two_runs_really_run_at_the_same_time(monkeypatch):
    monkeypatch.setenv("GEM_JOBS", "2")
    barrier = threading.Barrier(2, timeout=5)

    def overlapping_runner(gem_url, refresh, on_event):
        # Only two jobs running together get past this; with one slot the
        # first times out and fails, and the test sees it.
        barrier.wait()
        return {"matches": [], "from_cache": False}

    client = TestClient(create_app(runner=overlapping_runner, demo=False))
    run_ids = [client.post("/api/runs", json={"gem_url": GEM_URL}).json()["run_id"] for _ in range(2)]
    for run_id in run_ids:
        assert wait_until_finished(client, run_id, timeout=8.0)["status"] == "done"


def test_without_gem_jobs_the_second_run_stays_queued(monkeypatch):
    monkeypatch.delenv("GEM_JOBS", raising=False)
    started, release = threading.Event(), threading.Event()

    def blocking_runner(gem_url, refresh, on_event):
        started.set()
        release.wait(5)
        return {"matches": [], "from_cache": False}

    client = TestClient(create_app(runner=blocking_runner, demo=False))
    first = client.post("/api/runs", json={"gem_url": GEM_URL}).json()["run_id"]
    assert started.wait(2)
    second = client.post("/api/runs", json={"gem_url": GEM_URL}).json()["run_id"]

    statuses = {r["id"]: r["status"] for r in client.get("/api/runs").json()["runs"]}
    assert statuses[first] == "running"
    assert statuses[second] == "queued"  # the one slot is held by the first run

    release.set()
    assert wait_until_finished(client, second)["status"] == "done"


def test_a_bad_gem_jobs_value_is_a_warning_not_a_crash(monkeypatch):
    monkeypatch.setenv("GEM_JOBS", "loads")
    client = TestClient(create_app(runner=fake_runner, demo=False))
    run_id = client.post("/api/runs", json={"gem_url": GEM_URL}).json()["run_id"]
    assert wait_until_finished(client, run_id)["status"] == "done"

# --- Stage 8: the API survives a restart and trims big traces --------------

def _saved_record(run_id="savedrun1"):
    return {
        "run_id": run_id,
        "gem_url": GEM_URL,
        "title": "Saved Mouse",
        "state": "done",
        "checked_at": "2026-10-20T10:00:00+00:00",
        "cheapest_price": 999.0,
        "match_count": 1,
        "from_cache": False,
        "result": {
            "result_version": 2,
            "state": "done",
            "gem": {"title": "Saved Mouse", "price": {"low": 1099.0, "high": 1099.0, "kind": "fixed"}},
            "shops": {},
            "matches": [],
            "cheapest": {"shop": "amazon.in", "price": 999.0, "saving_vs_gem": 100.0},
            "warnings": [],
        },
    }


def test_recent_runs_read_from_disk_after_a_restart():
    from core import runs as runs_store

    runs_store.save_run(_saved_record())
    restarted = TestClient(create_app(runner=fake_runner, demo=False))
    listed = restarted.get("/api/runs").json()["runs"]
    assert listed[0]["id"] == "savedrun1"
    assert listed[0]["title"] == "Saved Mouse"
    assert listed[0]["cheapest_price"] == 999.0
    assert listed[0]["live"] is False


def test_a_saved_run_can_be_reopened_after_a_restart():
    from core import runs as runs_store

    runs_store.save_run(_saved_record())
    restarted = TestClient(create_app(runner=fake_runner, demo=False))
    run = restarted.get("/api/runs/savedrun1").json()
    assert run["state"] == "done" and run["live"] is False
    assert run["result"]["cheapest"]["shop"] == "amazon.in"


def test_a_live_run_reports_the_v2_state_not_only_the_job_status():
    def shown_runner(gem_url, refresh, on_event):
        return {"state": "shown", "matches": [], "gem_product": {"title": "X"}}

    client = TestClient(create_app(runner=shown_runner, demo=False))
    run_id = client.post("/api/runs", json={"gem_url": GEM_URL}).json()["run_id"]
    run = wait_until_finished(client, run_id)
    assert run["state"] == "shown"


def test_the_trace_route_trims_saved_page_text(client):
    page = "x" * 5000
    trace = {
        "stages": {
            "extraction": {"raw_page_text": page},
            "matching": {"all_decisions": [{"candidate_url": "https://a.test/x", "raw_page_text": page}]},
        }
    }
    (pipeline.TRACES_DIR / "abcdef0123456789.json").write_text(json.dumps(trace), encoding="utf-8")
    body = client.get("/api/traces/abcdef0123456789").json()
    assert len(body["stages"]["extraction"]["raw_page_text"]) < 1200
    assert body["stages"]["extraction"]["raw_page_text"].startswith("x" * 1000)
    assert body["stages"]["matching"]["all_decisions"][0]["candidate_url"] == "https://a.test/x"


def test_gem_debug_keeps_the_whole_trace(client, monkeypatch):
    monkeypatch.setenv("GEM_DEBUG", "1")
    page = "y" * 5000
    (pipeline.TRACES_DIR / "abcdef0123456789.json").write_text(json.dumps({"raw_page_text": page}), encoding="utf-8")
    assert client.get("/api/traces/abcdef0123456789").json()["raw_page_text"] == page


def test_llm_status_marks_an_endpoint_that_used_up_its_allowance(client, monkeypatch):
    from core import config

    endpoint = Endpoint(provider="one", model="m", base_url="https://one.test/v1", api_key_env="K")
    state_file = config.CACHE_DIR / "llm_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"used_up": {endpoint.name: time.time() + 3600}}), encoding="utf-8")
    monkeypatch.setattr(router_module, "_router", Router([endpoint], strategy="spread"))

    body = client.get("/api/llm/status").json()
    assert body["endpoints"][0]["state"] == "limit"
    assert "used up until" in body["endpoints"][0]["reason"]
