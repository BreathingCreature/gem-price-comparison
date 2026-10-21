"""Tests for tools/free_models.py. No network: sessions and askers are injected."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402
from tools import free_models as fm  # noqa: E402

PRICE_OK = '{"price": 1299}'


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return _Resp(self.payload)


def _openrouter_entry(model_id, prompt="0", completion="0",
                     inputs=("text",), outputs=("text",), name=None, arch=True):
    entry = {
        "id": model_id,
        "name": name or model_id,
        "context_length": 1000,
        "pricing": {"prompt": prompt, "completion": completion},
    }
    if arch:
        entry["architecture"] = {
            "input_modalities": list(inputs),
            "output_modalities": list(outputs),
        }
    return entry


def test_discover_openrouter_keeps_only_free_text_models():
    session = _Session({"data": [
        _openrouter_entry("a/paid", prompt="0.5", completion="1"),
        _openrouter_entry("a/free-chat"),
        _openrouter_entry("a/free-audio", outputs=("text", "audio")),
        _openrouter_entry("a/free-guard", name="A Safety Guard"),
        _openrouter_entry("a/free-router"),
        _openrouter_entry(fm.OPENROUTER_AUTO_MODEL),
        _openrouter_entry("a/negative", prompt="-1"),
    ]})
    rows = fm.discover_openrouter(session)
    assert [row["id"] for row in rows] == ["a/free-chat", "a/free-router"]
    assert rows[0]["context"] == 1000


def test_discover_openrouter_tolerates_missing_architecture():
    session = _Session({"data": [_openrouter_entry("a/m", arch=False)]})
    assert [row["id"] for row in fm.discover_openrouter(session)] == ["a/m"]


def test_discover_nvidia_keeps_chat_families_best_first():
    session = _Session({"data": [
        {"id": "nvidia/llama-3.2-nv-embedqa-e5-v5"},
        {"id": "nvidia/llama-3.1-nemoguard-8b-content-safety"},
        {"id": "nvidia/nvclip"},
        {"id": "microsoft/phi-3-medium-128k-instruct"},
        {"id": "qwen/qwen3-coder-480b-a35b-instruct"},
        {"id": "meta/llama-3.3-70b-instruct"},
        {"id": "nvidia/nemotron-3-ultra-550b-a55b"},
    ]})
    rows = fm.discover_nvidia(session, api_key="")
    ids = [row["id"] for row in rows]
    assert ids[0] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert ids[1] == "meta/llama-3.3-70b-instruct"
    assert "nvidia/llama-3.2-nv-embedqa-e5-v5" not in ids
    assert "nvidia/llama-3.1-nemoguard-8b-content-safety" not in ids
    assert "nvidia/nvclip" not in ids


def test_discover_nvidia_ignores_a_missing_key_when_a_session_is_injected():
    session = _Session({"data": [{"id": "meta/llama-3.3-70b-instruct"}]})
    rows = fm.discover_nvidia(session)
    assert [row["id"] for row in rows] == ["meta/llama-3.3-70b-instruct"]


def test_probe_accepts_a_correct_json_price():
    row = fm.probe("m", lambda model, messages, max_tokens: PRICE_OK)
    assert row["ok"] is True
    assert row["status"] == "ok"
    assert row["seconds"] is not None


def test_probe_rejects_prose_and_reports_the_answer():
    row = fm.probe("m", lambda model, messages, max_tokens: "about 1299 rupees")
    assert row["ok"] is False
    assert "1299 rupees" in row["error"]


def test_probe_reports_a_failing_call_without_raising():
    class _Dead(OSError):
        status_code = 404

    def ask(model, messages, max_tokens):
        raise _Dead("model not found")

    row = fm.probe("gone", ask)
    assert row["ok"] is False
    assert row["status"] == "404"
    assert "model not found" in row["error"]


def test_write_models_changes_only_the_named_provider(tmp_path):
    path = tmp_path / "llm_providers.json"
    path.write_text(json.dumps({
        "strategy": "spread",
        "providers": [
            {"name": "nvidia", "base_url": config.NVIDIA_BASE_URL, "models": ["nvidia/old"]},
            {"name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "models": ["a/old:free"]},
        ],
    }), encoding="utf-8")

    fm.write_models("openrouter", ["a/x:free", "a/y:free"], path=path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["strategy"] == "spread"
    assert data["providers"][1]["models"] == ["a/x:free", "a/y:free"]
    assert data["providers"][0]["models"] == ["nvidia/old"]


def test_write_models_creates_a_missing_provider(tmp_path):
    path = tmp_path / "llm_providers.json"
    fm.write_models("openrouter", ["a/x:free"], path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data["providers"][0]
    assert entry["name"] == "openrouter"
    assert entry["api_key_env"] == "OPENROUTER_API_KEY"
    assert entry["models"] == ["a/x:free"]


def test_run_provider_applies_the_limit_and_probes(monkeypatch):
    monkeypatch.setattr(fm, "discover_openrouter", lambda session=None: [
        {"id": f"a/m{i}", "name": f"m{i}"} for i in range(5)
    ])
    seen = []

    def ask(model, messages, max_tokens):
        seen.append(model)
        return PRICE_OK

    rows, skipped = fm.run_provider("openrouter", limit=2, ask=ask)
    assert [row["model"] for row in rows] == ["a/m0", "a/m1"]
    assert skipped == 3
    assert sorted(seen) == ["a/m0", "a/m1"]


def test_run_provider_prefers_explicit_models(monkeypatch):
    def _no_discovery(*args, **kwargs):
        raise AssertionError("--models must skip discovery")

    monkeypatch.setattr(fm, "discover_openrouter", _no_discovery)
    rows, skipped = fm.run_provider(
        "openrouter", models=["a/one"], ask=lambda m, msgs, n: PRICE_OK
    )
    assert [row["model"] for row in rows] == ["a/one"]
    assert skipped == 0


def test_main_writes_the_fastest_correct_models(monkeypatch, capsys):
    rows = {
        "openrouter": [
            {"model": "slow", "ok": True, "seconds": 9.0, "status": "ok", "error": ""},
            {"model": "fast", "ok": True, "seconds": 1.0, "status": "ok", "error": ""},
            {"model": "dead", "ok": False, "seconds": 0.2, "status": "404", "error": "x"},
        ],
        "nvidia": [
            {"model": "nim", "ok": True, "seconds": 2.0, "status": "ok", "error": ""},
        ],
    }
    monkeypatch.setattr(fm, "run_provider", lambda name, **kwargs: (rows[name], 0))
    written = {}
    monkeypatch.setattr(fm, "write_models", lambda name, models: written.setdefault(name, models))

    assert fm.main(["--write", "--keep", "1"]) == 0

    assert written == {"openrouter": ["fast"], "nvidia": ["nim"]}
    out = capsys.readouterr().out
    assert "best: fast" in out and "best: nim" in out


def test_main_reports_nothing_good_with_a_failure_exit(monkeypatch, capsys):
    dead = [{"model": "x", "ok": False, "seconds": 0.1, "status": "404", "error": "gone"}]
    monkeypatch.setattr(fm, "run_provider", lambda name, **kwargs: (dead, 0))

    assert fm.main(["--provider", "openrouter"]) == 1
    out = capsys.readouterr().out
    assert "none (keep the current list)" in out


def test_main_json_report_has_rows_and_best(monkeypatch, capsys):
    rows = [{"model": "fast", "ok": True, "seconds": 1.0, "status": "ok", "error": ""}]
    monkeypatch.setattr(fm, "run_provider", lambda name, **kwargs: (rows, 4))

    assert fm.main(["--provider", "openrouter", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["openrouter"]["best"] == ["fast"]
    assert printed["openrouter"]["skipped"] == 4
