"""tools/probe_endpoints.py: one tiny call per endpoint, OK or the real error."""
from types import SimpleNamespace

from llm.router import Endpoint
from tools import probe_endpoints


def _endpoint(name, enabled=True, reason=""):
    return Endpoint(provider=name, model="m", base_url=f"https://{name}.test/v1",
                    api_key_env="K", enabled=enabled, disabled_reason=reason)


def _client(reply=None, error=None):
    class C:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs):
            if error:
                raise error
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])

    return C()


def test_a_working_endpoint_is_reported_ok():
    ep = _endpoint("nvidia")
    row = probe_endpoints.probe_endpoint(ep, client_factory=lambda e: _client(reply="OK"))
    assert row == {"endpoint": "nvidia/m", "ok": True, "seconds": row["seconds"], "reply": "OK"}
    assert row["seconds"] >= 0


def test_a_failing_endpoint_reports_the_real_error():
    ep = _endpoint("groq")
    row = probe_endpoints.probe_endpoint(ep, client_factory=lambda e: _client(error=RuntimeError("HTTP 401: bad key")))
    assert row["ok"] is False and "bad key" in row["error"]


def test_an_empty_answer_is_a_failure():
    ep = _endpoint("gemini")
    row = probe_endpoints.probe_endpoint(ep, client_factory=lambda e: _client(reply="   "))
    assert row["ok"] is False and "empty" in row["error"]


def test_a_disabled_endpoint_is_listed_with_its_reason_and_never_called():
    def explode(_e):
        raise AssertionError("a disabled endpoint must not be called")

    ep = _endpoint("x", enabled=False, reason="KEY_X is not set")
    row = probe_endpoints.probe_endpoint(ep, client_factory=explode)
    assert row["ok"] is False and row["disabled"] is True and "KEY_X is not set" in row["error"]


def test_main_prints_a_line_per_endpoint_and_fails_beyond_ok(monkeypatch, capsys):
    good, bad = _endpoint("good"), _endpoint("bad")
    monkeypatch.setattr(probe_endpoints, "load_endpoints", lambda: ([good, bad], "spread"))
    monkeypatch.setattr(probe_endpoints, "_make_client", lambda e: _client(reply="OK") if e is good else _client(error=RuntimeError("boom")))

    assert probe_endpoints.main([]) == 1  # something failed
    out = capsys.readouterr().out
    assert "good/m" in out and "OK" in out
    assert "bad/m" in out and "boom" in out


def test_main_is_quiet_and_returns_0_when_everything_works(monkeypatch):
    good = _endpoint("good")
    monkeypatch.setattr(probe_endpoints, "load_endpoints", lambda: ([good], "failover"))
    monkeypatch.setattr(probe_endpoints, "_make_client", lambda e: _client(reply="OK"))

    assert probe_endpoints.main([]) == 0
