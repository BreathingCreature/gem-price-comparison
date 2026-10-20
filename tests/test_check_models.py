"""Tests for tools/check_models.py. No network: the asker is injected."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402
from tools import check_models as cm  # noqa: E402

MATCH_OK = '{"same_product": true, "confidence": 0.95, "reason": "same model"}'
PRICE_OK = '{"price": 1299}'


def _asker(answers):
    """answers: {(model, "match"|"price"): raw text or an exception}"""

    def ask(model, messages, max_tokens):
        kind = "match" if messages is cm._MATCH else "price"
        value = answers[(model, kind)]
        if isinstance(value, Exception):
            raise value
        return value

    return ask


class _HTTPError(OSError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code = status


def test_a_good_model_answers_both_questions():
    row = cm.check_model("m", ask=_asker({("m", "match"): MATCH_OK, ("m", "price"): PRICE_OK}))
    assert row["ok"] is True
    assert row["json_ok"] and row["match_ok"] and row["price_ok"]
    assert row["status"] == "ok"


def test_fenced_json_and_a_formatted_price_still_count():
    fenced = '```json\n{"same_product": true, "confidence": 0.9, "reason": "ok"}\n```'
    price = '{"price": "\u20b91,299"}'
    row = cm.check_model("m", ask=_asker({("m", "match"): fenced, ("m", "price"): price}))
    assert row["ok"] is True


def test_a_wrong_answer_is_not_ok():
    row = cm.check_model("m", ask=_asker({
        ("m", "match"): '{"same_product": false, "confidence": 0.9, "reason": "no"}',
        ("m", "price"): PRICE_OK,
    }))
    assert row["ok"] is False
    assert row["match_ok"] is False


def test_prose_instead_of_json_is_not_ok():
    row = cm.check_model("m", ask=_asker({("m", "match"): "Yes, they look the same.", ("m", "price"): PRICE_OK}))
    assert row["json_ok"] is False
    assert row["ok"] is False


def test_a_404_is_reported_not_raised():
    row = cm.check_model("gone", ask=_asker({
        ("gone", "match"): _HTTPError(404, "model not found"),
        ("gone", "price"): _HTTPError(404, "model not found"),
    }))
    assert row["ok"] is False
    assert row["status"] == "404"
    assert "model not found" in row["error"]


def test_rank_keeps_only_good_models_fastest_first():
    rows = [
        {"model": "slow", "ok": True, "seconds": 9.0},
        {"model": "bad", "ok": False, "seconds": 0.4},
        {"model": "fast", "ok": True, "seconds": 1.2},
    ]
    assert cm.rank(rows) == ["fast", "slow"]


def test_write_config_changes_only_the_nvidia_entry(tmp_path):
    path = tmp_path / "llm_providers.json"
    path.write_text(json.dumps({
        "strategy": "spread",
        "providers": [
            {"name": "nvidia", "base_url": config.NVIDIA_BASE_URL, "api_key_env": "NVIDIA_API_KEY",
             "models": ["nvidia/old-model"]},
            {"name": "groq", "base_url": "https://api.groq.com/openai/v1", "api_key_env": "GROQ_API_KEY",
             "models": ["openai/gpt-oss-120b"]},
        ],
    }), encoding="utf-8")

    cm.write_config(["nvidia/a", "nvidia/b"], path=path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["strategy"] == "spread"
    assert data["providers"][0]["models"] == ["nvidia/a", "nvidia/b"]
    assert data["providers"][1]["models"] == ["openai/gpt-oss-120b"]


def test_write_config_creates_the_file_when_missing(tmp_path):
    path = tmp_path / "llm_providers.json"
    cm.write_config(["nvidia/a"], path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["providers"][0]["name"] == "nvidia"
    assert data["providers"][0]["models"] == ["nvidia/a"]


def test_forget_retired_drops_only_the_given_models():
    state_file = Path(config.CACHE_DIR) / "llm_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "used_up": {"nvidia/nvidia/a": 123.0},
        "retired": {
            "nvidia/nvidia/a": {"at": 1.0, "reason": "404"},
            "nvidia/nvidia/b": {"at": 2.0, "reason": "404"},
        },
    }), encoding="utf-8")

    dropped = cm.forget_retired(["nvidia/a"])

    assert dropped == ["nvidia/nvidia/a"]
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert set(saved["retired"]) == {"nvidia/nvidia/b"}
    assert saved["used_up"] == {"nvidia/nvidia/a": 123.0}


def test_main_reports_the_best_order(monkeypatch, capsys):
    rows = [
        {"model": "a", "ok": True, "json_ok": True, "match_ok": True, "price_ok": True,
         "seconds": 2.0, "status": "ok", "error": ""},
        {"model": "b", "ok": False, "json_ok": False, "match_ok": False, "price_ok": False,
         "seconds": 1.0, "status": "404", "error": "model not found"},
    ]
    monkeypatch.setattr(cm, "check_model", lambda model, timeout=None: rows[0 if model == "a" else 1])
    assert cm.main(["--models", "a,b", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["best"] == ["a"]


def test_main_fails_when_nothing_answers(monkeypatch, capsys):
    monkeypatch.setattr(cm, "check_model", lambda model, timeout=None: {
        "model": model, "ok": False, "json_ok": False, "match_ok": False, "price_ok": False,
        "seconds": 0.5, "status": "404", "error": "model not found",
    })
    assert cm.main(["--models", "x"]) == 1
    assert "no model answered" in capsys.readouterr().out
