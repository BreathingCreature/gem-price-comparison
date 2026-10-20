"""Key-shaped text never reaches a trace, an event, the API or a log."""
import json
import logging

import pytest

from core import events, pipeline
from core.config import SecretFilter
from core.errors import redact, redact_obj

KEY_SHAPED = [
    "nvapi-abc123DEF456",
    "gsk_abcdefghijklmnop",
    "AIzaSyA1234567890abcdefghijk",
    "sk-or-v1-0123456789abcdef",
    "sk-0123456789abcdefghijkl",
    "Bearer abc.def.ghi123456789",
    "api_key=SUPERSECRETVALUE123",
    "0123456789abcdef0123456789abcdef",
    "QWxzb1RoaXNPbmVMb25nQmFzZTY0U3RyaW5n",
]


@pytest.mark.parametrize("secret", KEY_SHAPED)
def test_key_shaped_text_is_hidden(secret):
    cleaned = redact(f"provider said: {secret} (401)")
    assert secret not in cleaned
    assert "[hidden]" in cleaned


def test_ordinary_text_is_untouched():
    text = "Checking 12 listings at https://example.com/x took 345 ms"
    assert redact(text) == text


def test_redact_obj_walks_nested_records():
    cleaned = redact_obj({"errors": ["bad nvapi-abc123DEF456"], "ok": 1, "nested": {"k": "gsk_abcdefghij"}})
    dumped = json.dumps(cleaned)
    assert "nvapi-" not in dumped and "gsk_" not in dumped
    assert cleaned["ok"] == 1


def test_events_hide_keys():
    with events.capture() as sink:
        events.emit("provider_failed", error="nvapi-abc123DEF456", errors=["gsk_abcdefghij"])
    dumped = json.dumps(sink.events)
    assert "nvapi-" not in dumped and "gsk_" not in dumped
    assert "[hidden]" in dumped


def test_the_trace_file_hides_keys():
    pipeline._write_trace({"trace_id": "secret123", "error": "boom gsk_abcdefghij", "events": []})
    text = (pipeline.TRACES_DIR / "secret123.json").read_text(encoding="utf-8")
    assert "gsk_" not in text
    assert "[hidden]" in text


def test_a_provider_error_with_a_key_never_reaches_the_trace_or_events():
    url = "https://mkp.gem.gov.in/test/secret/p-1-1-cat.html"
    seen = []

    def normalize_fn(_product):
        raise RuntimeError("Incorrect API key provided: nvapi-abc123DEF456")

    with pytest.raises(RuntimeError):
        pipeline.run_pipeline(
            url,
            refresh=True,
            on_event=seen.append,
            extract_fn=lambda _url: {"title": "Mouse", "gem_price": 1000.0},
            normalize_fn=normalize_fn,
            flipkart_search_fn=lambda _query: None,
        )

    traces = list(pipeline.TRACES_DIR.glob("*.json"))
    assert traces, "the failed run should still leave a trace"
    text = traces[0].read_text(encoding="utf-8")
    assert "nvapi-" not in text
    assert "nvapi-" not in json.dumps(seen)
    assert "[hidden]" in text


def test_the_log_filter_scrubs_a_key_and_keeps_percent_args_working():
    record = logging.LogRecord("t", logging.WARNING, __file__, 1, "failed with %s", ("nvapi-abc123DEF456",), None)
    SecretFilter().filter(record)
    assert "nvapi-" not in record.getMessage()

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "took %d ms", (5,), None)
    SecretFilter().filter(record)
    assert record.getMessage() == "took 5 ms"
