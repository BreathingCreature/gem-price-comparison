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
    monkeypatch.setenv("GEM_DEMO_SPEED", "0")
    client = TestClient(create_app(demo=True))
    assert client.get("/api/health").json()["demo"] is True

    run_id = client.post("/api/runs", json={"gem_url": "https://example.com/anything"}).json()["run_id"]
    run = wait_until_finished(client, run_id)

    assert run["status"] == "done" and len(run["result"]["matches"]) == 4
    seen, _ = read_stream(client, run_id)
    types = {e["type"] for e in seen}
    assert {"gem_product", "search_finished", "candidate_verdict", "llm_call", "llm_cooldown", "run_finished"} <= types


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
