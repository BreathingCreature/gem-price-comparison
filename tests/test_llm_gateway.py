"""Stage 5 — the LLM gateway: endpoint health that survives a restart.

Three new behaviours, all about being honest when the free LLM plans run
out:

- An endpoint that answers "daily limit" / "quota exceeded" is remembered
  as used up until the reset (or the next local midnight) in
  cache/llm_state.json, so a restart does not walk straight back into it.
- "Nobody can answer" now has two flavours: GatewayBusy (cooling down, try
  again shortly) and GatewayExhausted (every allowance is gone for today).
  Both still subclass RuntimeError, so every older caller keeps working.
- An empty answer counts as a failed call: retried once on ANOTHER
  endpoint, and never written to the answer cache.

No real network or API key is involved — every client below is a fake.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402
from core import pipeline  # noqa: E402
from llm import cache as llm_cache  # noqa: E402
from llm import client as llm_client  # noqa: E402
from llm import router as router_module  # noqa: E402
from llm.match import MatchBatchResult  # noqa: E402
from llm.router import Endpoint, GatewayBusy, GatewayExhausted, Router  # noqa: E402
from scrapers._base import CandidateResult, SearchScrapeResult  # noqa: E402

MESSAGES = [{"role": "user", "content": "hi"}]
WALL_START = datetime(2026, 10, 20, 15, 30, 0).timestamp()  # a local afternoon
MIDNIGHT = datetime(2026, 10, 21, 0, 0, 0).timestamp()


def state_file() -> Path:
    return Path(config.CACHE_DIR) / "llm_state.json"


class _HTTPError(OSError):
    """Shaped like an openai/httpx failure: a status code and, sometimes,
    response headers."""

    def __init__(self, status, message, headers=None):
        super().__init__(message)
        self.status_code = status
        self.response = SimpleNamespace(headers=headers or {})


class FakeClient:
    """One scripted answer per call. An entry is a string (the answer), ""
    (an empty answer) or an exception to raise."""

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


class Clocks:
    def __init__(self, wall=WALL_START):
        self.now = 1000.0
        self.wall = wall

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def wall_clock(self):
        return self.wall


def _router(clients, clocks=None, **kwargs):
    clocks = clocks or Clocks()
    endpoints = kwargs.pop("endpoints", None) or [
        Endpoint(provider=name, model="m", base_url=f"https://{name}.test/v1", api_key_env="FAKE_KEY")
        for name in clients
    ]
    router = Router(
        endpoints,
        clock=clocks.monotonic,
        sleep=clocks.sleep,
        wall_clock=clocks.wall_clock,
        client_factory=lambda ep: clients[ep.provider],
        **kwargs,
    )
    return router, clocks


# --- used up for the day, remembered across restarts -----------------------------

def test_a_daily_limit_marks_the_endpoint_used_up_until_midnight():
    client = FakeClient([_HTTPError(429, "daily limit reached for this model")])
    router, _ = _router({"a": client})

    with pytest.raises(GatewayExhausted) as err:
        router.complete(MESSAGES, 100)

    assert client.calls == 1  # no immediate retry against a used-up endpoint
    assert "used up until" in str(err.value)
    saved = json.loads(state_file().read_text(encoding="utf-8"))
    assert set(saved["used_up"]) == {"a/m"}
    assert saved["used_up"]["a/m"] == pytest.approx(MIDNIGHT)


def test_a_restart_still_skips_an_endpoint_whose_day_is_used_up():
    first = FakeClient([_HTTPError(402, "quota exceeded")])
    router, _ = _router({"a": first})
    with pytest.raises(GatewayExhausted):
        router.complete(MESSAGES, 100)

    fresh = FakeClient(["answer from a"], label="a")
    second, _ = _router({"a": fresh})
    with pytest.raises(GatewayExhausted):
        second.complete(MESSAGES, 100)

    assert fresh.calls == 0  # the restart remembered, it did not try again


def test_a_reset_hint_in_the_error_sets_the_wake_up_time():
    client = FakeClient([_HTTPError(429, "quota exceeded, retry after 2 hours")])
    router, clocks = _router({"a": client})

    with pytest.raises(GatewayExhausted):
        router.complete(MESSAGES, 100)

    saved = json.loads(state_file().read_text(encoding="utf-8"))
    assert saved["used_up"]["a/m"] == pytest.approx(clocks.wall + 2 * 3600)


def test_a_momentary_rate_limit_is_not_a_used_up_day():
    client = FakeClient([_HTTPError(429, "too many requests, slow down")])
    router, clocks = _router({"a": client}, max_wait=5.0)

    with pytest.raises(GatewayBusy):
        router.complete(MESSAGES, 100)

    assert not state_file().exists()  # nothing was written to disk
    assert not router._is_used_up(router.endpoints[0])


def test_the_used_up_file_holds_endpoint_names_only():
    client = FakeClient([_HTTPError(429, "daily quota exhausted")])
    router, _ = _router({"a": client})
    with pytest.raises(GatewayExhausted):
        router.complete(MESSAGES, 100)

    text = state_file().read_text(encoding="utf-8")
    assert "a/m" in text
    assert "FAKE_KEY" not in text
    assert "api_key" not in text


def test_the_state_file_is_replaced_in_one_piece(monkeypatch):
    import os

    real_replace = os.replace
    calls = []
    monkeypatch.setattr(os, "replace", lambda src, dst: (calls.append((str(src), str(dst))), real_replace(src, dst))[1])

    client = FakeClient([_HTTPError(429, "daily limit")])
    router, _ = _router({"a": client})
    with pytest.raises(GatewayExhausted):
        router.complete(MESSAGES, 100)

    assert len(calls) == 1
    source, destination = calls[0]
    assert Path(destination) == state_file()
    assert source != destination  # a temp file next to it, then one rename
    assert list(Path(config.CACHE_DIR).glob(".llm_state_*")) == []


def test_status_reports_a_used_up_endpoint():
    client = FakeClient([_HTTPError(429, "daily limit")])
    router, _ = _router({"a": client})
    with pytest.raises(GatewayExhausted):
        router.complete(MESSAGES, 100)

    row = router.status()[0]
    assert row["state"] == "limit"
    assert "used up until 00:00" in row["reason"]


# --- busy vs. exhausted -----------------------------------------------------------

def test_one_cooling_endpoint_is_busy_not_exhausted():
    client = FakeClient([_HTTPError(503, "service unavailable")])
    router, _ = _router({"a": client}, max_wait=5.0)

    with pytest.raises(GatewayBusy) as err:
        router.complete(MESSAGES, 100)

    assert "busy or failing" in str(err.value)


def test_everything_used_up_names_the_endpoints_and_the_time():
    client = FakeClient([_HTTPError(429, "daily limit")])
    router, _ = _router({"a": client})

    with pytest.raises(GatewayExhausted) as err:
        router.complete(MESSAGES, 100)

    assert str(err.value) == "LLM limits used up until 00:00 for: a/m"


def test_the_two_gateway_errors_are_still_runtime_errors():
    assert issubclass(GatewayBusy, RuntimeError)
    assert issubclass(GatewayExhausted, RuntimeError)


# --- empty answers ---------------------------------------------------------------

def test_an_empty_answer_is_retried_once_on_another_endpoint():
    a = FakeClient([""], label="a")
    b = FakeClient(["answer from b"], label="b")
    router, _ = _router({"a": a, "b": b})

    assert router.complete(MESSAGES, 100) == "answer from b"
    assert (a.calls, b.calls) == (1, 1)


def test_two_empty_answers_give_up_as_busy():
    a = FakeClient([""], label="a")
    b = FakeClient([""], label="b")
    router, _ = _router({"a": a, "b": b})

    with pytest.raises(GatewayBusy) as err:
        router.complete(MESSAGES, 100)

    assert "empty answer" in str(err.value)
    assert (a.calls, b.calls) == (1, 1)


def test_an_empty_answer_is_never_cached(monkeypatch):
    monkeypatch.setattr(llm_cache, "ENABLED", True)
    monkeypatch.setattr(llm_cache, "CACHE_DIR", config.CACHE_DIR / "llm")
    monkeypatch.setattr(llm_client, "NVIDIA_MIN_SECONDS_BETWEEN_CALLS", 0.0)
    a = FakeClient([""], label="a")
    b = FakeClient([""], label="b")
    router, _ = _router({"a": a, "b": b})
    monkeypatch.setattr(router_module, "get_router", lambda: router)

    with pytest.raises(GatewayBusy):
        llm_client._call_llm("system", "user", max_tokens=10, purpose="verify")

    assert list((config.CACHE_DIR / "llm").glob("*.json")) == []


# --- the pipeline's side of the deal ----------------------------------------------

def _gem_product(url):
    return {
        "url": url,
        "category_slug": "computer-mouse",
        "product_slug": "logitech-m650",
        "title": "Logitech M650 Wireless Mouse",
        "brand": "Logitech",
        "model": None,
        "specs": {},
        "gem_price": 1299.0,
        "gem_price_type": "fixed",
        "currency": "INR",
    }


def _empty_search(domain):
    def search(query):
        return SearchScrapeResult(source_domain=domain, query=query, candidates=[])

    return search


def _no_expansion(normalized, tried_queries, tried_domains):
    return {"new_queries": [], "suggested_domains": [], "notes": ""}


def test_a_used_up_llm_at_normalize_is_a_warning_and_the_run_continues():
    seen = {}

    def flipkart_search_fn(query):
        seen.setdefault("queries", []).append(query)
        return SearchScrapeResult(source_domain="flipkart.com", query=query, candidates=[])

    def match_fn(normalized, candidates):
        seen["normalized"] = normalized
        return MatchBatchResult()

    def normalize_fn(gem_product):
        raise GatewayExhausted("LLM limits used up until 00:00 for: a/m")

    result = pipeline.run_pipeline(
        "https://mkp.gem.gov.in/test/llm-used-up/p-9-9-cat.html",
        refresh=True,
        extract_fn=_gem_product,
        normalize_fn=normalize_fn,
        flipkart_search_fn=flipkart_search_fn,
        amazon_search_fn=_empty_search("amazon.in"),
        discover_fn=lambda queries, domains: [],
        generic_scrape_fn=lambda url: (_ for _ in ()).throw(AssertionError("no URLs to scrape")),
        match_fn=match_fn,
        expand_search_fn=_no_expansion,
    )

    assert any("used up" in warning for warning in result["warnings"])
    # The run still searched, using the GeM title as the identity.
    assert seen["queries"] and seen["queries"][0] == "Logitech M650 Wireless Mouse"


def test_a_busy_llm_at_match_is_a_warning_and_the_listings_are_skipped():
    def flipkart_search_fn(query):
        return SearchScrapeResult(
            source_domain="flipkart.com",
            query=query,
            candidates=[
                CandidateResult(
                    source_domain="flipkart.com",
                    url="https://www.flipkart.com/m650/p/itm1",
                    scraper_used="flipkart",
                    structured_fields={"title": "Logitech M650", "price": 1199.0},
                    raw_page_text="",
                )
            ],
        )

    def match_fn(normalized, candidates):
        raise GatewayBusy("All LLM endpoints are busy or failing: a/m: cooling down")

    result = pipeline.run_pipeline(
        "https://mkp.gem.gov.in/test/llm-busy/p-8-8-cat.html",
        refresh=True,
        extract_fn=_gem_product,
        normalize_fn=lambda gem_product: {
            "canonical_name": "Logitech M650",
            "brand": "Logitech",
            "model": "M650",
            "key_identifiers": ["M650"],
            "search_queries": ["Logitech M650 wireless mouse"],
        },
        flipkart_search_fn=flipkart_search_fn,
        amazon_search_fn=_empty_search("amazon.in"),
        discover_fn=lambda queries, domains: [],
        generic_scrape_fn=lambda url: (_ for _ in ()).throw(AssertionError("no URLs to scrape")),
        match_fn=match_fn,
        expand_search_fn=_no_expansion,
    )

    assert result["matches"] == []
    assert any("busy" in warning for warning in result["warnings"])
    assert "https://www.flipkart.com/m650/p/itm1" in result["skipped"]


# --- a model that 404s is retired, and the next model takes over -----------------

def _save_retired(at: float, name: str = "a/m", reason: str = "HTTP 404: not found") -> None:
    state_file().parent.mkdir(parents=True, exist_ok=True)
    state_file().write_text(
        json.dumps({"used_up": {}, "retired": {name: {"at": at, "reason": reason}}}),
        encoding="utf-8",
    )


def test_a_404_uses_the_next_model_and_is_remembered_on_disk():
    dead = FakeClient([_HTTPError(404, "model not found")])
    live = FakeClient(["answer from b"], label="b")
    router, _ = _router({"a": dead, "b": live})

    assert router.complete(MESSAGES, 100) == "answer from b"
    assert dead.calls == 1
    assert not router.endpoints[0].enabled
    assert "404" in router.endpoints[0].disabled_reason
    saved = json.loads(state_file().read_text(encoding="utf-8"))
    assert set(saved["retired"]) == {"a/m"}
    assert "reason" in saved["retired"]["a/m"]


def test_a_restart_skips_a_retired_model_without_calling_it():
    dead = FakeClient([_HTTPError(404, "model not found")])
    router, _ = _router({"a": dead, "b": FakeClient(["first"], label="b")})
    assert router.complete(MESSAGES, 100) == "first"

    fresh = FakeClient(["never used"], label="a")
    second, _ = _router({"a": fresh, "b": FakeClient(["second"], label="b")})
    assert second.complete(MESSAGES, 100) == "second"
    assert fresh.calls == 0
    rows = {row["endpoint"]: row for row in second.status()}
    assert rows["a/m"]["retired"] is True
    assert "404" in rows["a/m"]["reason"]


def test_a_retired_model_is_tried_again_after_the_recheck_window(monkeypatch):
    monkeypatch.setattr(config, "RETIRED_RECHECK_DAYS", 1.0)
    clocks = Clocks()
    _save_retired(at=clocks.wall - 2 * 86400)

    client = FakeClient(["it came back"], label="a")
    router, _ = _router({"a": client}, clocks=clocks)
    assert router.endpoints[0].enabled
    assert router.complete(MESSAGES, 100) == "it came back"
    assert json.loads(state_file().read_text(encoding="utf-8"))["retired"] == {}


def test_a_retired_entry_newer_than_the_window_keeps_the_model_off(monkeypatch):
    monkeypatch.setattr(config, "RETIRED_RECHECK_DAYS", 7.0)
    clocks = Clocks()
    _save_retired(at=clocks.wall - 3600)

    client = FakeClient(["must not be called"], label="a")
    router, _ = _router({"a": client}, clocks=clocks)
    assert not router.endpoints[0].enabled
    assert client.calls == 0


def test_a_working_model_keeps_the_used_up_note_in_the_same_file():
    dead = FakeClient([_HTTPError(404, "model not found")])
    live = FakeClient([_HTTPError(429, "daily limit")])
    router, _ = _router({"a": dead, "b": live})
    with pytest.raises(GatewayExhausted):
        router.complete(MESSAGES, 100)

    saved = json.loads(state_file().read_text(encoding="utf-8"))
    assert "a/m" in saved["retired"]
    assert saved["used_up"]["b/m"] == pytest.approx(MIDNIGHT)


def test_the_endpoint_name_does_not_repeat_the_provider():
    assert Endpoint(provider="nvidia", model="nvidia/nemotron-x", base_url="https://x.test/v1",
                    api_key_env="K").name == "nvidia/nemotron-x"
    assert Endpoint(provider="groq", model="openai/gpt-oss-120b", base_url="https://x.test/v1",
                    api_key_env="K").name == "groq/openai/gpt-oss-120b"


def test_an_old_doubled_name_in_the_state_file_still_retires_the_model():
    state_file().parent.mkdir(parents=True, exist_ok=True)
    state_file().write_text(json.dumps({
        "used_up": {},
        "retired": {"nvidia/nvidia/nemotron-x": {"at": Clocks().wall, "reason": "404"}},
    }), encoding="utf-8")
    clients = {"nvidia": FakeClient(["never used"], label="nvidia")}
    router, _ = _router(clients, endpoints=[
        Endpoint(provider="nvidia", model="nvidia/nemotron-x", base_url="https://nvidia.test/v1",
                 api_key_env="FAKE_KEY"),
    ])

    assert router.endpoints[0].name == "nvidia/nemotron-x"
    assert not router.endpoints[0].enabled
    assert clients["nvidia"].calls == 0
