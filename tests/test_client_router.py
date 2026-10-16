"""llm.client sends its calls through the router unless a client is passed in."""
from types import SimpleNamespace

from core import events
from llm import client as llm_client
from llm import router as router_module
from llm.router import Endpoint, Router


def _reply(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class ScriptedClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.sent.append(kwargs)
        return _reply(self.answers.pop(0))


def _install_router(monkeypatch, client):
    ep = Endpoint(provider="fake", model="fake-model", base_url="https://fake.test/v1", api_key_env="K")
    router = Router([ep], client_factory=lambda _ep: client)
    monkeypatch.setattr(router_module, "_router", router)
    return router


def test_call_llm_without_a_client_goes_through_the_router(monkeypatch):
    client = ScriptedClient('{"ok": true}')
    _install_router(monkeypatch, client)

    assert llm_client._call_llm("system", "user") == {"ok": True}
    assert client.sent[0]["model"] == "fake-model"
    assert client.sent[0]["messages"][0] == {"role": "system", "content": "system"}


def test_the_json_correction_retry_also_goes_through_the_router(monkeypatch):
    client = ScriptedClient("not json", '{"fixed": 1}')
    _install_router(monkeypatch, client)

    assert llm_client._call_llm("system", "user") == {"fixed": 1}
    assert len(client.sent) == 2


def test_plain_providers_do_not_get_nvidia_only_options(monkeypatch):
    client = ScriptedClient('{"ok": true}')
    _install_router(monkeypatch, client)

    llm_client._call_llm("system", "user")
    assert "extra_body" not in client.sent[0]


def test_nvidia_style_still_gets_its_options():
    assert "extra_body" in llm_client._model_kwargs("nvidia/nemotron-3-super-120b-a12b")
    assert "extra_body" not in llm_client._model_kwargs("nvidia/nemotron-3-super-120b-a12b", style="plain")


def test_the_purpose_of_each_call_is_reported(monkeypatch):
    client = ScriptedClient(
        '{"canonical_name": "Demo", "brand": null, "model": null, "key_identifiers": [], "search_queries": ["demo"]}'
    )
    _install_router(monkeypatch, client)

    seen = []
    with events.capture(seen.append):
        llm_client.normalize_product({"title": "Demo", "category_slug": "x", "product_slug": "y"})

    call = next(e for e in seen if e["type"] == "llm_call")
    assert call["purpose"] == "normalize" and call["ok"] is True


def test_a_client_passed_in_still_bypasses_the_router(monkeypatch):
    def explode():
        raise AssertionError("the router must not be used when a client is given")

    monkeypatch.setattr(router_module, "get_router", explode)
    fake = ScriptedClient('{"ok": true}')

    assert llm_client._call_llm("system", "user", client=fake) == {"ok": True}
