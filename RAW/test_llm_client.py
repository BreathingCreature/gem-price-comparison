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

import llm_client  # noqa: E402

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
