"""
Tests for llm_client.py.

No real NVIDIA API calls happen here — this sandbox has no key configured
and can't reach integrate.api.nvidia.com anyway (network's locked to a
fixed domain allowlist). Every function that calls the LLM takes an
optional `client=` param specifically so tests can inject a fake one
shaped like the real OpenAI client instead. This proves the prompt-
building, response-parsing, retry, and fallback-merging logic is correct.
It does NOT prove the real model actually returns useful answers to these
prompts — that needs a real key and a real run.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm import client as llm_client  # noqa: E402

# Kill the rate-limit sleep for tests — otherwise every multi-call test adds
# real wall-clock delay for no benefit.
llm_client.NVIDIA_MIN_SECONDS_BETWEEN_CALLS = 0.0


# --- fake OpenAI-shaped client --------------------------------------------------

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


# --- _extract_json ---------------------------------------------------------------

def test_extract_json_plain():
    assert llm_client._extract_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_extract_json_markdown_fenced():
    raw = '```json\n{"a": 1}\n```'
    assert llm_client._extract_json(raw) == {"a": 1}


def test_extract_json_stray_prose_around_object():
    raw = 'Sure, here is the JSON:\n{"a": 1, "nested": {"b": 2}}\nHope that helps!'
    assert llm_client._extract_json(raw) == {"a": 1, "nested": {"b": 2}}


def test_extract_json_no_object_returns_none():
    assert llm_client._extract_json("no json here at all") is None


def test_extract_json_handles_braces_inside_strings():
    raw = '{"text": "value with { curly } braces inside"}'
    assert llm_client._extract_json(raw) == {"text": "value with { curly } braces inside"}


# --- normalize_product -----------------------------------------------------------

def test_normalize_product_uses_llm_output():
    fake = _FakeClient(
        '{"brand": "Logitech", "model": "M650", "key_identifiers": ["M650", "Bluetooth"], '
        '"canonical_name": "Logitech M650 — Wireless Mouse", "search_queries": ["Logitech M650 wireless mouse"]}'
    )
    gem_product = {
        "title": "Logitech M650 Wireless Mouse",
        "category_slug": "computer-mouse",
        "product_slug": "logitech-m650-mouse-bt-usb",
        "brand": "Logitech",  # Part 1's rough guess
        "model": None,
        "specs": {"Connectivity": "Bluetooth + USB"},
    }
    result = llm_client.normalize_product(gem_product, client=fake)
    assert result["brand"] == "Logitech"
    assert result["model"] == "M650"
    assert "Bluetooth" in result["key_identifiers"]
    assert result["search_queries"] == ["Logitech M650 wireless mouse"]


def test_normalize_product_falls_back_to_gem_fields_when_llm_returns_null():
    fake = _FakeClient('{"brand": null, "model": null, "key_identifiers": [], "canonical_name": null, "search_queries": []}')
    gem_product = {
        "title": "Unbranded Office Chair",
        "category_slug": "office-chair",
        "product_slug": "unbranded-office-chair",
        "brand": "Unbranded",
        "model": None,
        "specs": {},
    }
    result = llm_client.normalize_product(gem_product, client=fake)
    # LLM gave null brand -> falls back to Part 1's guess, not left empty
    assert result["brand"] == "Unbranded"
    assert result["canonical_name"] == "Unbranded Office Chair"  # falls back to raw title
    assert result["search_queries"] == ["Unbranded Office Chair"]  # falls back to title as a last resort


# --- normalize_product prompt-size cap (E1) -------------------------------------------

def _last_normalize_prompt(fake):
    return fake.chat.completions.calls[0]["messages"][1]["content"]


def test_normalize_product_small_specs_prompt_is_unchanged():
    # E1 regression guard: with few short specs the prompt must be exactly
    # the same text the old join-everything formatting produced.
    fake = _FakeClient('{"search_queries": ["q"]}')
    gem_product = {
        "title": "Logitech M650 Wireless Mouse",
        "category_slug": "computer-mouse",
        "product_slug": "logitech-m650-mouse-bt-usb",
        "specs": {"Connectivity": "Bluetooth + USB", "Colour": "Graphite"},
    }
    llm_client.normalize_product(gem_product, client=fake)
    assert _last_normalize_prompt(fake) == (
        'title: "Logitech M650 Wireless Mouse"\n'
        'slug_words: "computer mouse logitech m650 mouse bt usb"\n'
        "specs:\n"
        "- Connectivity: Bluetooth + USB\n"
        "- Colour: Graphite"
    )


def test_normalize_product_caps_specs_at_40_and_flags_omitted():
    fake = _FakeClient('{"search_queries": ["q"]}')
    specs = {f"Spec {i}": f"value {i}" for i in range(100)}
    gem_product = {"title": "T", "category_slug": "c", "product_slug": "p", "specs": specs}
    llm_client.normalize_product(gem_product, client=fake)
    prompt = _last_normalize_prompt(fake)
    spec_lines = [ln for ln in prompt.splitlines() if ln.startswith("- ")]
    assert len(spec_lines) == 41  # 40 kept rows + the omitted flag
    assert spec_lines[-1] == "- (60 more specs omitted)"
    assert "- Spec 0: value 0" in spec_lines
    assert "- Spec 39: value 39" in spec_lines


def test_normalize_product_cuts_long_spec_values():
    fake = _FakeClient('{"search_queries": ["q"]}')
    gem_product = {
        "title": "T", "category_slug": "c", "product_slug": "p",
        "specs": {"Detail": "x" * 1000, "Colour": "Graphite"},
    }
    llm_client.normalize_product(gem_product, client=fake)
    prompt = _last_normalize_prompt(fake)
    assert "- Detail: " + "x" * 200 in prompt
    assert "x" * 201 not in prompt
    assert "- Colour: Graphite" in prompt


def test_normalize_product_cuts_long_spec_keys():
    fake = _FakeClient('{"search_queries": ["q"]}')
    gem_product = {
        "title": "T", "category_slug": "c", "product_slug": "p",
        "specs": {"K" * 1000: "v"},
    }
    llm_client.normalize_product(gem_product, client=fake)
    prompt = _last_normalize_prompt(fake)
    assert "- " + "K" * 80 + ": v" in prompt
    assert "K" * 81 not in prompt


def test_normalize_product_skips_empty_spec_rows_and_reports_omitted():
    fake = _FakeClient('{"search_queries": ["q"]}')
    gem_product = {
        "title": "T", "category_slug": "c", "product_slug": "p",
        "specs": {"Empty": "", "OK": "fine"},
    }
    llm_client.normalize_product(gem_product, client=fake)
    prompt = _last_normalize_prompt(fake)
    assert "Empty" not in prompt
    assert "- OK: fine" in prompt
    assert "- (1 more specs omitted)" in prompt


# --- verify_or_extract -------------------------------------------------------------

def test_verify_or_extract_trusts_scraper_fields():
    fake = _FakeClient(
        '{"is_match": true, "confidence": 0.92, "reason": "Same brand and model.", '
        '"extracted_title": "Logitech M650 Wireless Mouse", "extracted_price": 1299, "extraction_source": "scraper"}'
    )
    normalized = {"canonical_name": "Logitech M650", "brand": "Logitech", "model": "M650", "key_identifiers": ["M650"]}
    candidate = {
        "url": "https://www.flipkart.com/logitech-m650/p/itm123",
        "source_domain": "flipkart.com",
        "structured_fields": {"title": "Logitech M650 Wireless Mouse", "price": 1299.0},
        "raw_page_text": "Logitech M650 Wireless Mouse ₹1,299",
    }
    decision = llm_client.verify_or_extract(normalized, candidate, client=fake)
    assert decision["is_match"] is True
    assert decision["confidence"] == 0.92
    assert decision["price_used"] == 1299.0
    assert decision["extraction_source"] == "scraper"
    assert decision["candidate_url"] == candidate["url"]


def test_verify_or_extract_falls_back_to_raw_read_when_structured_fields_missing():
    fake = _FakeClient(
        '{"is_match": true, "confidence": 0.7, "reason": "Matched from page text despite missing scraper fields.", '
        '"extracted_title": "Logitech M650", "extracted_price": 1349, "extraction_source": "llm_raw_read"}'
    )
    normalized = {"canonical_name": "Logitech M650", "brand": "Logitech", "model": "M650", "key_identifiers": ["M650"]}
    candidate = {
        "url": "https://brandstore.example.com/m650",
        "source_domain": "brandstore.example.com",
        "structured_fields": None,  # scraper found nothing structured
        "raw_page_text": "Buy the Logitech M650 wireless mouse today for just Rs. 1,349 — free shipping.",
    }
    decision = llm_client.verify_or_extract(normalized, candidate, client=fake)
    assert decision["extraction_source"] == "llm_raw_read"
    assert decision["price_used"] == 1349.0


def test_verify_or_extract_no_match_has_no_price():
    fake = _FakeClient(
        '{"is_match": false, "confidence": 0.85, "reason": "Different model number.", '
        '"extracted_title": "Logitech M330", "extracted_price": 899, "extraction_source": "scraper"}'
    )
    normalized = {"canonical_name": "Logitech M650", "brand": "Logitech", "model": "M650", "key_identifiers": ["M650"]}
    candidate = {
        "url": "https://www.amazon.in/dp/B0XYZ",
        "source_domain": "amazon.in",
        "structured_fields": {"title": "Logitech M330", "price": 899.0},
        "raw_page_text": "",
    }
    decision = llm_client.verify_or_extract(normalized, candidate, client=fake)
    assert decision["is_match"] is False
    assert decision["price_used"] is None  # not surfaced when it's not actually a match


# --- confidence clamp -----------------------------------------------------------

_NORMALIZED = {"canonical_name": "X", "brand": "B", "model": "M", "key_identifiers": ["M"]}
_CANDIDATE = {"url": "https://example.com/x", "source_domain": "example.com", "structured_fields": None, "raw_page_text": ""}


def _verify(confidence_value):
    fake = _FakeClient(
        '{"is_match": true, "confidence": %s, "reason": "x", "extracted_title": "t", "extracted_price": null, "extraction_source": "scraper"}' % confidence_value
    )
    return llm_client.verify_or_extract(_NORMALIZED, _CANDIDATE, client=fake)


def test_verify_confidence_85_percent_becomes_0_85():
    assert _verify(85)["confidence"] == 0.85


def test_verify_confidence_40_percent_becomes_0_4():
    assert _verify(40)["confidence"] == 0.4


def test_verify_confidence_100_percent_becomes_1_0():
    assert _verify(100)["confidence"] == 1.0


def test_verify_confidence_already_fraction_unchanged():
    assert _verify(0.9)["confidence"] == 0.9
    assert _verify(1.0)["confidence"] == 1.0
    assert _verify(0.0)["confidence"] == 0.0


def test_verify_confidence_out_of_range_is_zero():
    assert _verify(150)["confidence"] == 0.0
    assert _verify(-5)["confidence"] == 0.0


def test_verify_confidence_nan_is_zero(monkeypatch):
    monkeypatch.setattr(llm_client, "_extract_json", lambda raw: {"confidence": float("nan"), "is_match": True, "reason": "x", "extracted_title": "t", "extracted_price": None, "extraction_source": "scraper"})
    fake = _FakeClient("ignored - _extract_json is monkeypatched")
    assert llm_client.verify_or_extract(_NORMALIZED, _CANDIDATE, client=fake)["confidence"] == 0.0


def test_verify_confidence_inf_is_zero(monkeypatch):
    monkeypatch.setattr(llm_client, "_extract_json", lambda raw: {"confidence": float("inf"), "is_match": True, "reason": "x", "extracted_title": "t", "extracted_price": None, "extraction_source": "scraper"})
    fake = _FakeClient("ignored - _extract_json is monkeypatched")
    assert llm_client.verify_or_extract(_NORMALIZED, _CANDIDATE, client=fake)["confidence"] == 0.0


# --- expand_search -------------------------------------------------------------------

def test_expand_search_returns_new_queries_and_domains():
    fake = _FakeClient(
        '{"new_queries": ["Logitech M650 signature mouse"], "suggested_domains": ["reliancedigital.in"], '
        '"notes": "Logitech products are commonly available at Reliance Digital in India."}'
    )
    normalized = {"canonical_name": "Logitech M650", "brand": "Logitech", "model": "M650", "key_identifiers": ["M650"]}
    result = llm_client.expand_search(normalized, tried_queries=["Logitech M650"], tried_domains=["amazon.in", "flipkart.com"], client=fake)
    assert result["new_queries"] == ["Logitech M650 signature mouse"]
    assert "reliancedigital.in" in result["suggested_domains"]


# --- retry behaviour on bad JSON -----------------------------------------------------

def test_call_llm_retries_once_then_succeeds():
    fake = _FakeClient("not json at all", '{"ok": true}')
    result = llm_client._call_llm("system", "user", client=fake)
    assert result == {"ok": True}
    assert len(fake.chat.completions.calls) == 2  # confirms it actually retried, not a fluke pass


def test_call_llm_raises_after_failed_retry():
    fake = _FakeClient("still not json", "nope, also not json")
    try:
        llm_client._call_llm("system", "user", client=fake)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "valid JSON" in str(e)


# --- transient endpoint-error retry (NVIDIA 503 storms) ------------------------------

def test_complete_retries_same_model_on_transient_endpoint_error():
    # One blip on the (pinned, single) model must not kill the call —
    # the old code fell straight through to "next model" (of which there
    # is none) and raised.
    class _Transient(Exception):
        pass

    class _FlakyCompletions:
        def __init__(self):
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            if self.n == 1:
                raise _Transient("503 service temporarily overloaded")
            return _FakeResponse('{"ok": true}')

    class _FlakyChat:
        def __init__(self):
            self.completions = _FlakyCompletions()

    class _FlakyClient:
        def __init__(self):
            self.chat = _FlakyChat()

    orig_errors = llm_client._endpoint_errors
    orig_backoff = llm_client.ENDPOINT_RETRY_BACKOFF
    llm_client._endpoint_errors = lambda: (_Transient,)
    llm_client.ENDPOINT_RETRY_BACKOFF = (0.0, 0.0)  # no real sleeping in tests
    try:
        client = _FlakyClient()
        result = llm_client._call_llm("system", "user", client=client)
        assert result == {"ok": True}
        assert client.chat.completions.n == 2  # failed once, retried same model, succeeded
    finally:
        llm_client._endpoint_errors = orig_errors
        llm_client.ENDPOINT_RETRY_BACKOFF = orig_backoff


def test_complete_raises_after_all_transient_attempts_exhausted():
    class _AlwaysDown(Exception):
        pass

    class _DeadCompletions:
        def __init__(self):
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            raise _AlwaysDown("503 forever")

    class _DeadChat:
        def __init__(self):
            self.completions = _DeadCompletions()

    class _DeadClient:
        def __init__(self):
            self.chat = _DeadChat()

    orig_errors = llm_client._endpoint_errors
    orig_backoff = llm_client.ENDPOINT_RETRY_BACKOFF
    orig_attempts = llm_client.ENDPOINT_RETRY_ATTEMPTS
    llm_client._endpoint_errors = lambda: (_AlwaysDown,)
    llm_client.ENDPOINT_RETRY_BACKOFF = (0.0, 0.0)
    llm_client.ENDPOINT_RETRY_ATTEMPTS = 2
    try:
        client = _DeadClient()
        try:
            llm_client._call_llm("system", "user", client=client)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "All LLM models failed" in str(e)
        assert client.chat.completions.n == 2  # exactly the configured attempts, then give up
    finally:
        llm_client._endpoint_errors = orig_errors
        llm_client.ENDPOINT_RETRY_BACKOFF = orig_backoff
        llm_client.ENDPOINT_RETRY_ATTEMPTS = orig_attempts


# --- B1: confidence parsing ------------------------------------------------

def test_complete_raises_after_all_transient_attempts_exhausted():
    class _AlwaysDown(Exception):
        pass

    class _DeadCompletions:
        def __init__(self):
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            raise _AlwaysDown("503 forever")

    class _DeadChat:
        def __init__(self):
            self.completions = _DeadCompletions()

    class _DeadClient:
        def __init__(self):
            self.chat = _DeadChat()

    orig_errors = llm_client._endpoint_errors
    orig_backoff = llm_client.ENDPOINT_RETRY_BACKOFF
    orig_attempts = llm_client.ENDPOINT_RETRY_ATTEMPTS
    llm_client._endpoint_errors = lambda: (_AlwaysDown,)
    llm_client.ENDPOINT_RETRY_BACKOFF = (0.0, 0.0)
    llm_client.ENDPOINT_RETRY_ATTEMPTS = 2
    try:
        client = _DeadClient()
        try:
            llm_client._call_llm("system", "user", client=client)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "All LLM models failed" in str(e)
        assert client.chat.completions.n == 2  # exactly the configured attempts, then give up
    finally:
        llm_client._endpoint_errors = orig_errors
        llm_client.ENDPOINT_RETRY_BACKOFF = orig_backoff
        llm_client.ENDPOINT_RETRY_ATTEMPTS = orig_attempts


# --- B1: confidence parsing ------------------------------------------------

def test_confidence_0_9_stays_0_9():
    assert llm_client._parse_confidence(0.9) == 0.9


def test_confidence_1_0_stays_1_0():
    assert llm_client._parse_confidence(1.0) == 1.0


def test_confidence_0_stays_0():
    assert llm_client._parse_confidence(0) == 0.0


def test_confidence_85_percent_divided():
    assert llm_client._parse_confidence(85) == 0.85


def test_confidence_40_percent_divided():
    assert llm_client._parse_confidence(40) == 0.4


def test_confidence_100_percent_divided():
    assert llm_client._parse_confidence(100) == 1.0


def test_confidence_string_95_percent():
    assert llm_client._parse_confidence("95%") == 0.95


def test_confidence_string_0_8():
    assert llm_client._parse_confidence("0.8") == 0.8


def test_confidence_bool_true_is_zero():
    assert llm_client._parse_confidence(True) == 0.0


def test_confidence_bool_false_is_zero():
    assert llm_client._parse_confidence(False) == 0.0


def test_confidence_none_is_zero():
    assert llm_client._parse_confidence(None) == 0.0


def test_confidence_nan_is_zero():
    assert llm_client._parse_confidence(float("nan")) == 0.0


def test_confidence_inf_is_zero():
    assert llm_client._parse_confidence(float("inf")) == 0.0


def test_confidence_negative_is_zero():
    assert llm_client._parse_confidence(-3) == 0.0


def test_confidence_above_100_is_zero():
    assert llm_client._parse_confidence(150) == 0.0


# --- B5: config settings helpers -------------------------------------------

def test_flag_empty_means_default(monkeypatch):
    import importlib
    import core.config as config_mod

    monkeypatch.setenv("GEM_PARALLEL", "")
    importlib.reload(config_mod)
    assert config_mod.PARALLEL_SEARCH is False  # default

    monkeypatch.setenv("GEM_PARALLEL", "1")
    importlib.reload(config_mod)
    assert config_mod.PARALLEL_SEARCH is True

    # Restore for other tests
    monkeypatch.setenv("GEM_PARALLEL", "0")
    importlib.reload(config_mod)


def test_float_setting_invalid_uses_default(monkeypatch):
    import importlib
    import core.config as config_mod

    monkeypatch.setenv("PRICE_SANITY_LOW_RATIO", "not-a-number")
    importlib.reload(config_mod)
    assert config_mod.PRICE_SANITY_LOW_RATIO == 0.15  # default

    monkeypatch.setenv("PRICE_SANITY_LOW_RATIO", "0.5")
    importlib.reload(config_mod)
    assert config_mod.PRICE_SANITY_LOW_RATIO == 0.5

    monkeypatch.setenv("PRICE_SANITY_LOW_RATIO", "0.15")
    importlib.reload(config_mod)


def test_int_setting_invalid_uses_default(monkeypatch):
    import importlib
    import core.config as config_mod

    monkeypatch.setenv("NVIDIA_MAX_TOKENS", "not-a-number")
    importlib.reload(config_mod)
    assert config_mod.NVIDIA_MAX_TOKENS == 2000  # default

    monkeypatch.setenv("NVIDIA_MAX_TOKENS", "4000")
    importlib.reload(config_mod)
    assert config_mod.NVIDIA_MAX_TOKENS == 4000

    monkeypatch.setenv("NVIDIA_MAX_TOKENS", "2000")
    importlib.reload(config_mod)


# --- end B5 ---


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
