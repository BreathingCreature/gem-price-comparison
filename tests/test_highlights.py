"""Tests for llm/highlights.py and its pipeline/result plumbing.

No network and no real LLM: every call goes through an injected fake
client shaped like the OpenAI one (see tests/test_llm_client.py for the
same pattern). The grounding gate, the deterministic fallback, the cache
and the background job are all exercised through compute_highlights /
start_highlights / run_pipeline with fakes.
"""
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import pipeline  # noqa: E402
from core import result as result_module  # noqa: E402
from llm import client as llm_client  # noqa: E402
from llm import highlights  # noqa: E402
from llm.match import MatchBatchResult  # noqa: E402
from scrapers._base import CandidateResult, SearchScrapeResult  # noqa: E402

# Kill the rate-limit sleep for tests - otherwise every fake call could
# add real wall-clock delay for no benefit.
llm_client.NVIDIA_MIN_SECONDS_BETWEEN_CALLS = 0.0


# --- fake OpenAI-shaped clients ------------------------------------------------------

class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)  # queue, one string per expected call
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake client got more calls than responses queued")
        return _FakeResponse(self._responses.pop(0))


class _FakeChat:
    def __init__(self, responses):
        self.completions = _FakeCompletions(responses)


class _FakeClient:
    def __init__(self, *responses):
        self.chat = _FakeChat(responses)


class _MustNotCallCompletions:
    """Any call raises; a cache hit must return before that happens."""

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("the LLM must not be called on a cache hit")


class _MustNotCallChat:
    def __init__(self):
        self.completions = _MustNotCallCompletions()


class _MustNotCallClient:
    def __init__(self):
        self.chat = _MustNotCallChat()


class _BlockingCompletions:
    def __init__(self, gate):
        self._gate = gate
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        self._gate.wait()  # the test never sets this: the job never finishes
        raise AssertionError("the gate was never supposed to be set")


class _BlockingChat:
    def __init__(self, gate):
        self.completions = _BlockingCompletions(gate)


class _BlockingClient:
    def __init__(self, gate):
        self.chat = _BlockingChat(gate)


# --- products, answers and helpers -----------------------------------------------------

_GEM = {
    "title": "Logitech M650 Wireless Mouse",
    "brand": "Logitech",
    "model": "M650",
    "category_slug": "computer-mouse",
    "specs": {
        "Connectivity": "Bluetooth + USB",
        "Colour": "Graphite",
        "Battery": "AA battery",
    },
}

_GOOD_FACTS = [
    {"label": "Connectivity", "value": "Bluetooth + USB"},
    {"label": "Colour", "value": "Graphite"},
    {"label": "Battery", "value": "AA battery"},
    {"label": "Brand", "value": "Logitech"},
]

_GOOD_ANSWER = json.dumps({"title": "Logitech M650 Wireless Mouse", "facts": _GOOD_FACTS})


def _compute(gem, *responses):
    """Run the full path with a clean cache entry and a queued fake client."""
    highlights._cache_path(gem).unlink(missing_ok=True)
    return highlights.compute_highlights(gem, client=_FakeClient(*responses))


# --- the grounding gate ---------------------------------------------------------------

def test_a_good_answer_passes_the_gate_and_keeps_the_order():
    result = _compute(_GEM, _GOOD_ANSWER)
    assert result["source"] == "llm"
    assert result["facts"] == _GOOD_FACTS  # every row grounded, order preserved


def test_a_hallucinated_value_is_dropped():
    # "2 Year Warranty" appears nowhere in the title or the specs, so the
    # words and the digit-run cannot be found in the haystack.
    facts = _GOOD_FACTS + [{"label": "Warranty", "value": "2 Year Warranty"}]
    answer = json.dumps({"title": "Logitech M650 Wireless Mouse", "facts": facts})
    result = _compute(_GEM, answer)
    assert result["source"] == "llm"
    assert result["facts"] == _GOOD_FACTS  # the four grounded rows survive


def test_a_value_of_forty_characters_or_more_is_dropped():
    exactly_forty = "Graphite Bluetooth USB  Battery AA Mouse"  # 40 chars, all grounded
    over_long = "Bluetooth USB Graphite Battery AA Wireless"  # 42 chars, all grounded
    assert len(exactly_forty) == 40 and len(over_long) == 42
    facts = _GOOD_FACTS + [
        {"label": "Summary A", "value": exactly_forty},
        {"label": "Summary B", "value": over_long},
    ]
    answer = json.dumps({"title": "Logitech M650 Wireless Mouse", "facts": facts})
    result = _compute(_GEM, answer)
    assert result["source"] == "llm"
    assert result["facts"] == _GOOD_FACTS  # only the length rule can drop them


def test_an_empty_facts_list_falls_back():
    answer = json.dumps({"title": "Logitech M650 Wireless Mouse", "facts": []})
    result = _compute(_GEM, answer)
    assert result["source"] == "fallback"
    assert result["facts"] == highlights.fallback_highlights(_GEM)
    assert result["facts"][0] == {"label": "Brand", "value": "Logitech"}


def test_malformed_json_falls_back_without_raising():
    client = _FakeClient("sure, here you go", "still not json at all")
    highlights._cache_path(_GEM).unlink(missing_ok=True)
    result = highlights.compute_highlights(_GEM, client=client)
    assert result["source"] == "fallback"
    assert result["facts"] == highlights.fallback_highlights(_GEM)
    assert len(client.chat.completions.calls) == 2  # first try + the one correction retry


def test_the_heading_must_name_the_brand_or_model():
    answer = json.dumps({"title": "Generic Wireless Peripheral", "facts": _GOOD_FACTS})
    result = _compute(_GEM, answer)
    assert result["source"] == "fallback"  # the whole answer is rejected


def test_fewer_than_four_surviving_facts_falls_back():
    answer = json.dumps({"title": "Logitech M650 Wireless Mouse", "facts": _GOOD_FACTS[:3]})
    result = _compute(_GEM, answer)
    assert result["source"] == "fallback"
    assert result["facts"] == highlights.fallback_highlights(_GEM)


def test_the_prompt_carries_the_identity_and_the_specs():
    client = _FakeClient(_GOOD_ANSWER)
    highlights._cache_path(_GEM).unlink(missing_ok=True)
    highlights.compute_highlights(_GEM, client=client)
    call = client.chat.completions.calls[0]
    system = call["messages"][0]["content"]
    user = call["messages"][1]["content"]
    assert '{"title": "short card heading"' in system  # the JSON shape is asked for
    assert "at most 14 entries" in system
    assert 'title: "Logitech M650 Wireless Mouse"' in user
    assert 'brand: "Logitech"' in user
    assert 'model: "M650"' in user
    assert 'category_slug: "computer-mouse"' in user
    assert "- Connectivity: Bluetooth + USB" in user
    assert call["max_tokens"] == highlights.MAX_TOKENS


# --- the deterministic fallback ---------------------------------------------------------

def test_fallback_orders_brand_then_model_then_specs():
    gem = {
        "title": "Acme Z100 Scanner",
        "brand": "Acme",
        "model": "Z100",
        "specs": {"colour": "black", "weight": "1 kg"},
    }
    assert highlights.fallback_highlights(gem) == [
        {"label": "Brand", "value": "Acme"},
        {"label": "Model", "value": "Z100"},
        {"label": "Colour", "value": "black"},
        {"label": "Weight", "value": "1 kg"},
    ]


def test_fallback_drops_procurement_noise_and_empty_values():
    gem = {
        "title": "Acme Z100 Scanner",
        "brand": "Acme",
        "model": None,
        "specs": {
            "Seller Name": "Govt Supplier",
            "GeM Product Id": "GEM12345",
            "Product Id": "P-9",
            "Catalogue ID": "CAT-9",
            "Delivery": "7 days",
            "Payment": "15 days",
            "Colour": "",
            "Weight": "1 kg",
        },
    }
    assert highlights.fallback_highlights(gem) == [
        {"label": "Brand", "value": "Acme"},
        {"label": "Weight", "value": "1 kg"},
    ]


def test_fallback_keeps_at_most_eight_rows():
    specs = {f"spec {i}": f"value {i}" for i in range(20)}
    gem = {"title": "T", "brand": "Acme", "model": "Z100", "specs": specs}
    facts = highlights.fallback_highlights(gem)
    assert highlights.FALLBACK_FACTS == 8
    assert len(facts) == 8
    assert facts[0] == {"label": "Brand", "value": "Acme"}
    assert facts[1] == {"label": "Model", "value": "Z100"}


def test_fallback_title_cases_the_raw_spec_labels():
    gem = {
        "title": "T",
        "brand": None,
        "model": None,
        "specs": {"compatible with": "USB", "operating system": "Windows 11"},
    }
    facts = highlights.fallback_highlights(gem)
    assert facts == [
        {"label": "Compatible With", "value": "USB"},
        {"label": "Operating System", "value": "Windows 11"},
    ]


def test_country_of_origin_is_only_used_when_the_list_is_short():
    specs = {f"spec {i}": f"value {i}" for i in range(6)}
    specs["country of origin"] = "India"
    gem = {"title": "T", "brand": "Acme", "model": "Z100", "specs": specs}
    facts = highlights.fallback_highlights(gem)
    assert len(facts) == 8
    assert "Country Of Origin" not in [f["label"] for f in facts]  # six better rows filled it

    short = {
        "title": "T",
        "brand": "Acme",
        "model": None,
        "specs": {"colour": "black", "country of origin": "India"},
    }
    facts = highlights.fallback_highlights(short)
    assert [f["label"] for f in facts] == ["Brand", "Colour", "Country Of Origin"]


def test_fallback_never_raises_on_junk_input():
    assert highlights.fallback_highlights(None) == []
    assert highlights.fallback_highlights({"brand": "  ", "specs": None}) == []


# --- the background job and the cache ----------------------------------------------------

def test_a_job_that_never_finishes_reports_not_done():
    gate = threading.Event()
    highlights._cache_path(_GEM).unlink(missing_ok=True)
    job = highlights.start_highlights(dict(_GEM), client=_BlockingClient(gate))
    assert job.wait(0.1) is False  # the client is blocked on an event nobody sets
    assert job.done is False
    assert job.result() is None


def test_a_cache_hit_skips_the_llm_call():
    path = highlights._cache_path(_GEM)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"facts": _GOOD_FACTS, "saved_at": "2026-11-21T00:00:00+00:00"}),
        encoding="utf-8",
    )
    client = _MustNotCallClient()
    result = highlights.compute_highlights(dict(_GEM), client=client)
    assert result["facts"] == _GOOD_FACTS
    assert result["source"] == "llm"
    assert client.chat.completions.calls == []


def test_a_second_call_with_the_same_product_reuses_the_cache():
    payload = dict(_GEM, specs=dict(_GEM["specs"]))
    path = highlights._cache_path(payload)
    path.unlink(missing_ok=True)  # start from a clean slate
    first = highlights.compute_highlights(payload, client=_FakeClient(_GOOD_ANSWER))
    assert first["source"] == "llm"
    assert path.exists()
    client = _MustNotCallClient()
    second = highlights.compute_highlights(payload, client=client)
    assert second["facts"] == first["facts"]
    assert second["source"] == "llm"
    assert client.chat.completions.calls == []


def test_start_highlights_copies_the_product_before_starting():
    gem = {
        "title": "Demo Wireless Mouse",
        "brand": "Demo",
        "model": None,
        "category_slug": "computer-mouse",
        "specs": {"Colour": "Black"},
    }
    original = json.loads(json.dumps(gem))

    def _evil(gem_copy):
        gem_copy["title"] = "hacked"
        gem_copy["specs"]["Colour"] = "hacked"
        return []

    job = highlights.start_highlights(gem, highlights_fn=_evil)
    assert job.wait(5.0) is True
    assert job.done is True
    assert job.result()["source"] == "fallback"  # an empty answer falls back
    assert gem == original  # the caller's dict was never touched


# --- pipeline integration ----------------------------------------------------------------

GEM_URL = "https://mkp.gem.gov.in/computer-mouse/demo-mouse/p-300-400-cat.html"


def _gem_product(url):
    return {
        "url": url, "category_slug": "computer-mouse", "product_slug": "demo-mouse",
        "title": "Demo Wireless Mouse", "brand": "Demo", "model": None, "specs": {},
        "gem_price": 1000.0, "gem_price_type": "fixed", "currency": "INR",
    }


def _normalized(_gem):
    return {
        "canonical_name": "Demo Mouse", "brand": "Demo", "model": None, "key_identifiers": ["Demo"],
        "search_queries": ["demo wireless mouse"], "gem_price": 1000.0,
    }


def _candidate(domain, url, title, price):
    return CandidateResult(
        source_domain=domain, url=url, scraper_used=domain.split(".")[0],
        structured_fields={"title": title, "price": price, "currency": "INR"}, raw_page_text="",
    )


def _search(domain, candidates):
    return lambda query: SearchScrapeResult(source_domain=domain, query=query, candidates=list(candidates))


def _match_all(_normalized_product, candidates):
    decisions = [
        {"candidate_url": c.url, "source_domain": c.source_domain, "is_match": True, "confidence": 0.9,
         "reason": "same", "price_used": c.structured_fields["price"], "extraction_source": "scraper"}
        for c in candidates
    ]
    return MatchBatchResult(all_decisions=decisions, confirmed_matches=list(decisions))


def _no_expansion(*_args):
    return {"new_queries": [], "suggested_domains": [], "notes": ""}


def _run(url=GEM_URL, **overrides):
    seen = []
    kwargs = dict(
        extract_fn=_gem_product,
        normalize_fn=_normalized,
        flipkart_search_fn=_search("flipkart.com", [_candidate("flipkart.com", "https://www.flipkart.com/a", "Demo Mouse A", 900.0)]),
        amazon_search_fn=_search("amazon.in", [_candidate("amazon.in", "https://www.amazon.in/b", "Demo Mouse B", 950.0)]),
        discover_fn=lambda queries, domains: [],
        generic_scrape_fn=lambda u: None,
        match_fn=_match_all,
        expand_search_fn=_no_expansion,
    )
    kwargs.update(overrides)
    result = pipeline.run_pipeline(url, on_event=seen.append, **kwargs)
    return result, seen


def _types(seen):
    return [e["type"] for e in seen]


def test_run_without_highlights_fn_emits_nothing_and_keeps_the_card_empty():
    result, seen = _run(url=GEM_URL + "?no-highlights")
    assert result["gem"]["highlights"] == []
    assert "gem_highlights" not in _types(seen)


_INJECTED_FACTS = [
    {"label": "Brand", "value": "Demo"},
    {"label": "Type", "value": "Wireless"},
    {"label": "Connectivity", "value": "USB"},
    {"label": "Form Factor", "value": "Mouse"},
]


def test_an_injected_highlights_fn_reaches_the_result_and_the_event():
    captured = {}

    def _injected(gem):
        captured["title"] = gem.get("title")
        return [dict(row) for row in _INJECTED_FACTS]

    result, seen = _run(url=GEM_URL + "?with-highlights", highlights_fn=_injected)
    types = _types(seen)
    assert captured["title"] == "Demo Wireless Mouse"  # called with the product dict
    assert "gem_highlights" in types
    # run_finished (state "shown") is emitted by first_result before finish
    # settles the job, so gem_highlights lands between it and run_updated.
    assert types.index("normalized") < types.index("run_finished")
    assert types.index("run_finished") < types.index("gem_highlights") < types.index("run_updated")
    event = next(e for e in seen if e["type"] == "gem_highlights")
    assert event["source"] == "llm"
    assert event["facts"] == _INJECTED_FACTS
    assert result["gem"]["highlights"] == _INJECTED_FACTS


def test_the_pipeline_gives_up_on_a_slow_highlights_job():
    gate = threading.Event()

    def _blocked_highlights(_gem):
        gate.wait()
        return []

    original = highlights.HIGHLIGHT_WAIT_SECONDS
    highlights.HIGHLIGHT_WAIT_SECONDS = 0.05
    try:
        result, seen = _run(url=GEM_URL + "?slow-highlights", highlights_fn=_blocked_highlights)
    finally:
        highlights.HIGHLIGHT_WAIT_SECONDS = original
        gate.set()  # release the daemon thread; its late answer is never read
    event = next(e for e in seen if e["type"] == "gem_highlights")
    assert event["source"] == "fallback"
    assert event["facts"] == [{"label": "Brand", "value": "Demo"}]
    assert result["gem"]["highlights"] == [{"label": "Brand", "value": "Demo"}]


# --- result plumbing ------------------------------------------------------------------------

def test_an_old_result_without_highlights_still_builds():
    # A result saved before this feature had no highlights key anywhere;
    # building a fresh one from the same inputs must still work.
    old_gem_product = {"title": "Old Thing", "brand": "Old", "specs": {"Colour": "Black"}}
    built = result_module.build_result(
        gem_url="https://mkp.gem.gov.in/old",
        gem_product=old_gem_product,
        normalized=None,
        shops=None,
        matches=None,
    )
    assert built["gem"]["highlights"] == []
    # The old positional call shape of _gem_view keeps working too.
    view = result_module._gem_view("https://mkp.gem.gov.in/old", old_gem_product, {}, None, None)
    assert view["highlights"] == []
    # And the field is forwarded when a caller does pass it.
    built = result_module.build_result(
        gem_url="https://mkp.gem.gov.in/old2",
        gem_product=old_gem_product,
        normalized=None,
        shops=None,
        matches=None,
        highlights=[{"label": "Brand", "value": "Old"}],
    )
    assert built["gem"]["highlights"] == [{"label": "Brand", "value": "Old"}]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
