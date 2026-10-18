"""The router picks an endpoint, rests the ones that fail, and explains itself."""
import json
from types import SimpleNamespace

import pytest

from core import events
from llm import router as router_module
from llm.router import Endpoint, Router, load_endpoints


class FakeHTTPError(OSError):
    """Looks like an openai/httpx failure: has a status code and optional headers."""

    def __init__(self, status, message="boom", headers=None):
        super().__init__(message)
        self.status_code = status
        self.response = SimpleNamespace(headers=headers or {})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeClient:
    """Answers from a script. Each entry is a string (the answer) or an exception to raise."""

    def __init__(self, script=None, label="x"):
        self.script = list(script or [])
        self.label = label
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        step = self.script.pop(0) if self.script else f"answer from {self.label}"
        if isinstance(step, Exception):
            raise step
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=step))])


def make_router(clients, *, strategy="failover", gaps=None, max_wait=30.0):
    """clients: {provider_name: FakeClient}. One endpoint per client."""
    clock = Clock()
    gaps = gaps or {}
    endpoints = [
        Endpoint(provider=name, model="m", base_url=f"https://{name}.test/v1", api_key_env="K", min_gap=gaps.get(name, 0.0))
        for name in clients
    ]
    router = Router(
        endpoints, strategy=strategy, max_wait=max_wait, clock=clock, sleep=clock.sleep,
        client_factory=lambda ep: clients[ep.provider],
    )
    return router, clock


MESSAGES = [{"role": "user", "content": "hi"}]


def test_failover_uses_the_first_endpoint_while_it_works():
    a, b = FakeClient(label="a"), FakeClient(label="b")
    router, _ = make_router({"a": a, "b": b})

    for _ in range(3):
        assert router.complete(MESSAGES, 100) == "answer from a"

    assert (a.calls, b.calls) == (3, 0)


def test_a_failing_endpoint_hands_over_to_the_next_in_the_same_call():
    a = FakeClient([FakeHTTPError(503)], label="a")
    b = FakeClient(label="b")
    router, _ = make_router({"a": a, "b": b})

    assert router.complete(MESSAGES, 100) == "answer from b"
    states = {row["provider"]: row["state"] for row in router.status()}
    assert states == {"a": "cooling", "b": "ready"}


def test_a_rested_endpoint_comes_back_after_its_cooldown():
    a = FakeClient([FakeHTTPError(429)], label="a")
    b = FakeClient(label="b")
    router, clock = make_router({"a": a, "b": b})

    router.complete(MESSAGES, 100)  # a fails, b answers
    assert router.complete(MESSAGES, 100) == "answer from b"  # a is still resting
    clock.now += router_module.RATE_LIMIT_COOLDOWN + 1
    assert router.complete(MESSAGES, 100) == "answer from a"


def test_retry_after_header_sets_the_cooldown():
    a = FakeClient([FakeHTTPError(429, headers={"retry-after": "7"})], label="a")
    b = FakeClient(label="b")
    router, _ = make_router({"a": a, "b": b})

    router.complete(MESSAGES, 100)
    row = next(r for r in router.status() if r["provider"] == "a")
    assert 6.5 <= row["cooldown_remaining"] <= 7.0


def test_repeated_failures_rest_the_endpoint_longer():
    a = FakeClient([FakeHTTPError(503)] * 3, label="a")
    b = FakeClient(label="b")
    router, clock = make_router({"a": a, "b": b})

    waits = []
    for _ in range(3):
        router.complete(MESSAGES, 100)
        waits.append(next(r for r in router.status() if r["provider"] == "a")["cooldown_remaining"])
        clock.now += 1000  # let it come back, so it gets tried (and fails) again
    assert waits[0] < waits[1] < waits[2]


def test_a_rejected_key_switches_off_the_whole_provider():
    router, _ = make_router({"a": FakeClient([FakeHTTPError(401)]), "b": FakeClient(label="b")})
    router.endpoints.append(Endpoint(provider="a", model="m2", base_url="https://a.test/v1", api_key_env="K"))

    assert router.complete(MESSAGES, 100) == "answer from b"
    a_rows = [r for r in router.status() if r["provider"] == "a"]
    assert [r["state"] for r in a_rows] == ["disabled", "disabled"]
    assert "401" in a_rows[0]["reason"]


def test_a_missing_model_only_switches_off_that_endpoint():
    clients = {"a": FakeClient([FakeHTTPError(404)]), "b": FakeClient(label="b")}
    router, _ = make_router(clients)

    router.complete(MESSAGES, 100)
    states = {r["provider"]: r["state"] for r in router.status()}
    assert states == {"a": "disabled", "b": "ready"}


def test_an_empty_answer_counts_as_a_failure():
    a = FakeClient(["   "], label="a")
    b = FakeClient(label="b")
    router, _ = make_router({"a": a, "b": b})

    assert router.complete(MESSAGES, 100) == "answer from b"


def test_spread_strategy_takes_turns():
    a, b = FakeClient(label="a"), FakeClient(label="b")
    router, clock = make_router({"a": a, "b": b}, strategy="spread")

    answers = []
    for _ in range(4):
        answers.append(router.complete(MESSAGES, 100))
        clock.now += 1
    assert answers == ["answer from a", "answer from b", "answer from a", "answer from b"]


def test_minimum_gap_sends_the_call_to_another_endpoint_instead_of_waiting():
    a, b = FakeClient(label="a"), FakeClient(label="b")
    router, clock = make_router({"a": a, "b": b}, gaps={"a": 5.0})

    assert router.complete(MESSAGES, 100) == "answer from a"
    assert router.complete(MESSAGES, 100) == "answer from b"  # a was used a moment ago
    clock.now += 6
    assert router.complete(MESSAGES, 100) == "answer from a"


def test_when_everything_is_resting_it_waits_for_the_first_to_return():
    a = FakeClient([FakeHTTPError(503)], label="a")
    router, clock = make_router({"a": a}, max_wait=60.0)
    start = clock.now

    assert router.complete(MESSAGES, 100) == "answer from a"
    assert clock.now - start >= router_module.OTHER_ERROR_COOLDOWN  # it slept through the rest


def test_it_gives_up_when_the_wait_would_be_too_long():
    a = FakeClient([FakeHTTPError(429)] * 5, label="a")
    router, _ = make_router({"a": a}, max_wait=5.0)

    with pytest.raises(RuntimeError) as err:
        router.complete(MESSAGES, 100)
    assert "busy or failing" in str(err.value)


def test_no_usable_endpoint_gives_a_clear_reason():
    router, _ = make_router({"a": FakeClient()})
    router.endpoints[0].enabled = False
    router.endpoints[0].disabled_reason = "K is not set"

    with pytest.raises(RuntimeError) as err:
        router.complete(MESSAGES, 100)
    assert "K is not set" in str(err.value)


def test_ordinary_bugs_are_not_swallowed():
    a = FakeClient([ValueError("a real bug")], label="a")
    router, _ = make_router({"a": a, "b": FakeClient(label="b")})

    with pytest.raises(ValueError):
        router.complete(MESSAGES, 100)


def test_every_attempt_is_reported_as_an_event():
    a = FakeClient([FakeHTTPError(503)], label="a")
    b = FakeClient(label="b")
    router, _ = make_router({"a": a, "b": b})

    seen = []
    with events.capture(seen.append):
        router.complete(MESSAGES, 100, purpose="verify")

    calls = [e for e in seen if e["type"] == "llm_call"]
    assert [(e["provider"], e["ok"]) for e in calls] == [("a", False), ("b", True)]
    assert all(e["purpose"] == "verify" for e in calls)
    assert any(e["type"] == "llm_cooldown" and e["endpoint"] == "a/m" for e in seen)


def test_status_counts_calls_and_errors():
    a = FakeClient([FakeHTTPError(503)], label="a")
    router, _ = make_router({"a": a, "b": FakeClient(label="b")})

    router.complete(MESSAGES, 100)
    rows = {r["provider"]: r for r in router.status()}
    assert rows["a"]["errors"] == 1 and rows["a"]["calls"] == 0
    assert rows["b"]["calls"] == 1 and rows["b"]["errors"] == 0
    assert "503" in rows["a"]["last_error"]


# --- reading llm_providers.json -----------------------------------------------------

def _write_config(tmp_path, data):
    path = tmp_path / "llm_providers.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_config_file_builds_one_endpoint_per_model(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_A", "real-key")
    path = _write_config(tmp_path, {"strategy": "spread", "providers": [
        {"name": "a", "base_url": "https://a.test/v1", "api_key_env": "KEY_A", "models": ["m1", "m2"]},
    ]})

    endpoints, strategy = load_endpoints(path)
    assert strategy == "spread"
    assert [e.name for e in endpoints] == ["a/m1", "a/m2"]
    assert all(e.enabled for e in endpoints)


def test_a_provider_without_its_key_is_listed_but_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("KEY_B", raising=False)
    path = _write_config(tmp_path, [
        {"name": "b", "base_url": "https://b.test/v1", "api_key_env": "KEY_B", "models": ["m"]},
    ])

    endpoints, strategy = load_endpoints(path)
    assert strategy == "failover"
    assert endpoints[0].enabled is False
    assert "KEY_B is not set" in endpoints[0].disabled_reason


def test_a_placeholder_key_counts_as_not_set(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_C", "your_key_here")
    path = _write_config(tmp_path, [{"name": "c", "base_url": "https://c.test/v1", "api_key_env": "KEY_C", "models": ["m"]}])

    endpoints, _ = load_endpoints(path)
    assert endpoints[0].enabled is False


def test_enabled_false_in_the_file_turns_a_provider_off(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_D", "real-key")
    path = _write_config(tmp_path, [
        {"name": "d", "base_url": "https://d.test/v1", "api_key_env": "KEY_D", "models": ["m"], "enabled": False},
    ])

    endpoints, _ = load_endpoints(path)
    assert endpoints[0].enabled is False
    assert "turned off" in endpoints[0].disabled_reason


def test_a_provider_missing_a_required_field_is_a_loud_error(tmp_path):
    path = _write_config(tmp_path, [{"name": "e", "base_url": "https://e.test/v1", "models": ["m"]}])

    with pytest.raises(ValueError) as err:
        load_endpoints(path)
    assert "api_key_env" in str(err.value)


def test_an_unknown_strategy_is_a_loud_error(tmp_path):
    path = _write_config(tmp_path, {"strategy": "random", "providers": []})

    with pytest.raises(ValueError):
        load_endpoints(path)


def test_without_a_config_file_it_falls_back_to_nvidia_only(tmp_path, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "real-key")
    endpoints, strategy = load_endpoints(tmp_path / "does-not-exist.json")

    assert strategy == "failover"
    assert {e.provider for e in endpoints} == {"nvidia"}
    assert endpoints[0].style == "nvidia" and endpoints[0].enabled


# --- B2: request_options -----------------------------------------------------

def test_request_options_must_be_a_dict(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_X", "real-key")
    path = _write_config(tmp_path, [{"name": "x", "base_url": "https://x.test/v1", "api_key_env": "KEY_X", "models": ["m"], "request_options": "not-a-dict"}])
    with pytest.raises(ValueError) as err:
        load_endpoints(path)
    assert "request_options" in str(err.value)
    assert "x" in str(err.value)


def test_request_options_merged_into_create_call(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_X", "real-key")
    path = _write_config(tmp_path, [{"name": "x", "base_url": "https://x.test/v1", "api_key_env": "KEY_X", "models": ["m"], "request_options": {"reasoning_effort": "low"}}])
    endpoints, _ = load_endpoints(path)
    ep = endpoints[0]
    from llm.client import _model_kwargs
    kwargs = _model_kwargs("m", "plain", ep.request_options)
    assert kwargs.get("reasoning_effort") == "low"
    assert kwargs.get("stream") is False


# --- per-provider limits: max_concurrent and daily_limit ----------------------

def _limit_router(clients, *, caps=None, limits=None, today="2026-01-01"):
    """Like make_router, but every endpoint gets the given limits."""
    clock = Clock()
    caps = caps or {}
    limits = limits or {}
    endpoints = [
        Endpoint(
            provider=name,
            model="m",
            base_url=f"https://{name}.test/v1",
            api_key_env="K",
            max_concurrent=caps.get(name, 0),
            daily_limit=limits.get(name, 0),
        )
        for name in clients
    ]
    router = Router(
        endpoints, clock=clock, sleep=clock.sleep,
        client_factory=lambda ep: clients[ep.provider], today=lambda: today,
    )
    return router, clock


def test_max_concurrent_holds_a_provider_until_a_slot_frees_up():
    ep = Endpoint(provider="a", model="m", base_url="https://a.test/v1", api_key_env="K", max_concurrent=1)
    router = Router([ep])

    first, wait = router._pick()
    assert first is ep and wait == 0.0
    second, wait = router._pick()  # the in-flight call has not finished yet
    assert second is None and wait and wait > 0
    router._release_slot(ep)  # what _call does when its call ends
    third, _ = router._pick()
    assert third is ep


def test_no_limit_is_the_default_and_changes_nothing():
    a, b = FakeClient(label="a"), FakeClient(label="b")
    router, _ = _limit_router({"a": a, "b": b})
    for _ in range(3):
        assert router.complete(MESSAGES, 100) == "answer from a"
    assert a.calls == 3


def test_daily_limit_moves_to_the_next_provider_after_n_calls():
    a, b = FakeClient(label="a"), FakeClient(label="b")
    router, _ = _limit_router({"a": a, "b": b}, limits={"a": 2})

    assert router.complete(MESSAGES, 100) == "answer from a"
    assert router.complete(MESSAGES, 100) == "answer from a"
    assert router.complete(MESSAGES, 100) == "answer from b"  # a is done for today
    assert a.calls == 2


def test_the_daily_count_starts_fresh_on_a_new_day():
    day = ["2026-01-01"]
    a = FakeClient(label="a")
    router, _ = _limit_router({"a": a}, limits={"a": 1})
    router._today = lambda: day[0]

    assert router.complete(MESSAGES, 100) == "answer from a"
    with pytest.raises(RuntimeError) as err:
        router.complete(MESSAGES, 100)
    assert "daily limit" in str(err.value)  # and it explains why
    day[0] = "2026-01-02"
    assert router.complete(MESSAGES, 100) == "answer from a"


def test_status_shows_todays_calls_and_the_limits():
    a, b = FakeClient(label="a"), FakeClient(label="b")
    router, _ = _limit_router({"a": a, "b": b}, caps={"a": 2}, limits={"a": 50})
    router.complete(MESSAGES, 100)

    row = next(r for r in router.status() if r["provider"] == "a")
    assert row["calls_today"] == 1 and row["daily_limit"] == 50
    assert row["in_flight"] == 0 and row["max_concurrent"] == 2


def test_the_limits_are_read_from_the_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_L", "real-key")
    path = _write_config(tmp_path, [{
        "name": "l", "base_url": "https://l.test/v1", "api_key_env": "KEY_L", "models": ["m"],
        "max_concurrent": 3, "daily_limit": 900,
    }])

    endpoints, _ = load_endpoints(path)
    assert endpoints[0].max_concurrent == 3 and endpoints[0].daily_limit == 900


def test_without_the_keys_there_are_no_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_L", "real-key")
    path = _write_config(tmp_path, [{"name": "l", "base_url": "https://l.test/v1", "api_key_env": "KEY_L", "models": ["m"]}])

    endpoints, _ = load_endpoints(path)
    assert endpoints[0].max_concurrent == 0 and endpoints[0].daily_limit == 0


def test_a_nonsense_limit_is_a_loud_error(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_L", "real-key")
    path = _write_config(tmp_path, [{
        "name": "l", "base_url": "https://l.test/v1", "api_key_env": "KEY_L", "models": ["m"],
        "daily_limit": "lots",
    }])

    with pytest.raises(ValueError) as err:
        load_endpoints(path)
    assert "daily_limit" in str(err.value) and "l" in str(err.value)


# --- B3: empty answer includes finish_reason ---------------------------------

def test_empty_answer_includes_finish_reason():
    class _ClientWithFinishReason:
        def __init__(self, label):
            self.label = label
            self.calls = 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs):
            self.calls += 1
            choice = SimpleNamespace(message=SimpleNamespace(content=""), finish_reason="length")
            return SimpleNamespace(choices=[choice])

    a = _ClientWithFinishReason(label="a")
    router, _ = make_router({"a": a})

    with pytest.raises(RuntimeError) as err:
        router.complete(MESSAGES, 100)
    assert "finish_reason=length" in str(err.value)


