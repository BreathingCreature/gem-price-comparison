"""serve.py starts the page only when the offline checks pass and an LLM is ready."""
import pytest

import serve


class FakeSocket:
    def __init__(self, busy_port):
        self.busy_port = busy_port

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def bind(self, address):
        if address[1] == self.busy_port:
            raise OSError("in use")


def test_doctor_failure_stops_before_any_llm_check(monkeypatch, capsys):
    checked = []
    monkeypatch.setattr(serve, "doctor_passes", lambda: False)
    monkeypatch.setattr(serve, "ready_llm_count", lambda: checked.append(True) or 3)

    assert serve.main([]) == 1
    assert checked == []
    assert "doctor checks failed" in capsys.readouterr().err


def test_no_ready_endpoint_explains_how_to_add_a_key(monkeypatch, capsys):
    monkeypatch.setattr(serve, "doctor_passes", lambda: True)
    monkeypatch.setattr(serve, "ready_llm_count", lambda: 0)

    assert serve.main([]) == 1
    message = capsys.readouterr().err
    assert "No LLM endpoint is ready" in message
    assert "NVIDIA_API_KEY" in message
    assert "llm_providers.json" in message


def test_pick_port_skips_a_port_that_is_in_use(monkeypatch):
    monkeypatch.setattr(serve.socket, "socket", lambda *_args: FakeSocket(serve.DEFAULT_PORT))
    assert serve.pick_port() == serve.DEFAULT_PORT + 1


def test_pick_port_stops_when_the_range_is_full(monkeypatch):
    monkeypatch.setattr(serve.socket, "socket", lambda *_args: FakeSocket(serve.DEFAULT_PORT))
    with pytest.raises(SystemExit):
        serve.pick_port(tries=1)


def test_the_browser_opens_only_after_uvicorn_is_started(monkeypatch):
    opened = []
    monkeypatch.setattr(serve.webbrowser, "open", opened.append)

    class NotStarted:
        started = False

    serve.open_browser_when_ready(NotStarted(), 8123, timeout=0.01)
    assert opened == []

    class Started:
        started = True

    serve.open_browser_when_ready(Started(), 8123, timeout=0.01)
    assert opened == ["http://127.0.0.1:8123"]


def test_main_starts_uvicorn_on_the_chosen_port(monkeypatch):
    import uvicorn

    seen = {}
    ran = []

    class FakeServer:
        def __init__(self, config):
            seen["config"] = config

        def run(self):
            ran.append(True)

    class FakeThread:
        def __init__(self, target=None, args=(), **_kwargs):
            seen["target"] = target
            seen["args"] = args

        def start(self):
            seen["thread_started"] = True

    def fake_config(*_args, **kwargs):
        seen["options"] = kwargs
        return object()

    monkeypatch.setattr(serve, "doctor_passes", lambda: True)
    monkeypatch.setattr(serve, "ready_llm_count", lambda: 2)
    monkeypatch.setattr(serve, "pick_port", lambda: 8123)
    monkeypatch.setattr(uvicorn, "Config", fake_config)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(serve.threading, "Thread", FakeThread)

    assert serve.main([]) == 0
    assert ran == [True]
    assert seen["options"]["host"] == "127.0.0.1"
    assert seen["options"]["port"] == 8123
    assert seen["thread_started"] is True
