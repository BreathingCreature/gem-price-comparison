"""Saved LLM answers: used only when asked for, and only for valid answers."""
import importlib
import json
from pathlib import Path

import pytest

from llm import cache
from llm import client as llm_client


@pytest.fixture(autouse=True)
def cache_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "llm")


def test_key_changes_with_anything_that_changes_the_question():
    msgs = [{"role": "user", "content": "hi"}]
    base = cache.key_for(msgs, 100, "verify")
    assert base == cache.key_for([{"role": "user", "content": "hi"}], 100, "verify")
    assert base != cache.key_for([{"role": "user", "content": "hello"}], 100, "verify")
    assert base != cache.key_for(msgs, 200, "verify")
    assert base != cache.key_for(msgs, 100, "expand")


def test_put_then_get_and_clear():
    assert cache.get("abc") is None
    cache.put("abc", {"is_match": True})
    assert cache.get("abc") == {"is_match": True}
    assert cache.clear() == 1 and cache.get("abc") is None


def test_a_damaged_file_is_a_miss_not_a_crash(tmp_path):
    cache.put("abc", {"x": 1})
    (tmp_path / "llm" / "abc.json").write_text("{not json")
    assert cache.get("abc") is None


def _count_calls(monkeypatch, answers):
    calls = []

    def fake_ask(client, messages, max_tokens, purpose):
        calls.append(purpose)
        return answers.pop(0)

    monkeypatch.setattr(llm_client, "_ask", fake_ask)
    return calls


def test_off_by_default_every_question_reaches_the_llm(monkeypatch):
    monkeypatch.setattr(cache, "ENABLED", False)
    calls = _count_calls(monkeypatch, ['{"a": 1}', '{"a": 1}'])
    llm_client._call_llm("sys", "user", purpose="verify")
    llm_client._call_llm("sys", "user", purpose="verify")
    assert len(calls) == 2


def test_on_the_second_identical_question_is_answered_from_disk(monkeypatch):
    monkeypatch.setattr(cache, "ENABLED", True)
    calls = _count_calls(monkeypatch, ['{"a": 1}'])
    seen = []
    from core import events

    with events.capture(seen.append):
        first = llm_client._call_llm("sys", "user", purpose="verify")
        second = llm_client._call_llm("sys", "user", purpose="verify")
    assert first == second == {"a": 1}
    assert calls == ["verify"]
    assert [e["type"] for e in seen] == ["llm_cache_hit"]


def test_a_non_json_first_answer_is_not_saved_but_the_good_retry_is(monkeypatch):
    monkeypatch.setattr(cache, "ENABLED", True)
    calls = _count_calls(monkeypatch, ["sorry, no json", '{"ok": true}'])
    assert llm_client._call_llm("sys", "user", purpose="expand") == {"ok": True}
    assert len(calls) == 2
    assert llm_client._call_llm("sys", "user", purpose="expand") == {"ok": True}
    assert len(calls) == 2  # the saved answer was used


def test_a_client_passed_in_never_uses_the_cache(monkeypatch):
    monkeypatch.setattr(cache, "ENABLED", True)
    cache.put(cache.key_for([{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}], llm_client.NVIDIA_MAX_TOKENS, "verify"), {"stale": 1})
    calls = _count_calls(monkeypatch, ['{"fresh": 1}'])
    assert llm_client._call_llm("sys", "user", purpose="verify", client=object()) == {"fresh": 1}
    assert calls == ["verify"]


# --- B4: LLM_CACHE_TAG -------------------------------------------------------

def test_cache_key_changes_with_llm_cache_tag(monkeypatch):
    import importlib
    import core.config as config_mod

    msgs = [{"role": "user", "content": "hi"}]

    # Default (no tag)
    key1 = cache.key_for(msgs, 100, "verify")

    # With tag
    monkeypatch.setenv("LLM_CACHE_TAG", "exp-a")
    importlib.reload(config_mod)
    importlib.reload(cache)
    try:
        key2 = cache.key_for(msgs, 100, "verify")

        assert key1 != key2

        # Same tag gives same key
        key3 = cache.key_for(msgs, 100, "verify")
        assert key2 == key3
    finally:
        # Do not leave the tagged cache key (or its config) behind for other tests.
        monkeypatch.delenv("LLM_CACHE_TAG", raising=False)
        importlib.reload(config_mod)
        importlib.reload(cache)


def test_put_stores_model_name(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "llm")
    cache.put("test-key", {"answer": "hello"}, model="gpt-4")
    path = Path(cache.CACHE_DIR) / "test-key.json"
    data = json.loads(path.read_text())
    assert data["model"] == "gpt-4"
    assert data["answer"] == {"answer": "hello"}
