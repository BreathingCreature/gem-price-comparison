"""
Tests for matcher.py. Same fake-client approach as test_llm_client.py —
no real API calls. This tests the filter/sort/skip logic in match_candidates
itself; verify_or_extract's own prompt/parsing correctness is already
covered by test_llm_client.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matcher  # noqa: E402
import llm_client  # noqa: E402

matcher.MATCH_CONFIDENCE_THRESHOLD = 0.75  # deterministic regardless of .env
llm_client.NVIDIA_MIN_SECONDS_BETWEEN_CALLS = 0.0  # don't actually wait between fake calls


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
        self._responses = list(responses)

    def create(self, **kwargs):
        if not self._responses:
            raise AssertionError("fake client got more calls than responses queued")
        return _FakeResponse(self._responses.pop(0))


class _FakeChat:
    def __init__(self, responses):
        self.completions = _FakeCompletions(responses)


class _FakeClient:
    def __init__(self, *responses):
        self.chat = _FakeChat(responses)


def _candidate(url, domain="flipkart.com"):
    return {"url": url, "source_domain": domain, "structured_fields": {"title": "x"}, "raw_page_text": ""}


def _decision_json(is_match, confidence, price):
    return (
        f'{{"is_match": {str(is_match).lower()}, "confidence": {confidence}, "reason": "r", '
        f'"extracted_title": "t", "extracted_price": {price if price is not None else "null"}, '
        f'"extraction_source": "scraper"}}'
    )


# --- filtering ---------------------------------------------------------------------

def test_match_candidates_keeps_only_confirmed_above_threshold():
    fake = _FakeClient(
        _decision_json(True, 0.9, 1500),   # confirmed
        _decision_json(True, 0.95, 999),   # confirmed
        _decision_json(False, 0.99, 500),  # rejected — not a match regardless of confidence
        _decision_json(True, 0.5, 300),    # rejected — matched but below threshold
    )
    candidates = [_candidate(f"https://x.com/{i}") for i in range(4)]
    result = matcher.match_candidates({"canonical_name": "Demo"}, candidates, client=fake)

    assert len(result.all_decisions) == 4  # every candidate gets a decision recorded
    assert len(result.confirmed_matches) == 2
    assert {d["price_used"] for d in result.confirmed_matches} == {1500.0, 999.0}


def test_match_candidates_sorts_confirmed_by_price_ascending():
    fake = _FakeClient(
        _decision_json(True, 0.9, 1500),
        _decision_json(True, 0.9, 500),
        _decision_json(True, 0.9, 999),
    )
    candidates = [_candidate(f"https://x.com/{i}") for i in range(3)]
    result = matcher.match_candidates({"canonical_name": "Demo"}, candidates, client=fake)

    prices = [d["price_used"] for d in result.confirmed_matches]
    assert prices == [500.0, 999.0, 1500.0]


def test_match_candidates_confirmed_with_no_price_sorts_last():
    fake = _FakeClient(
        _decision_json(True, 0.9, None),  # matched but no price extracted
        _decision_json(True, 0.9, 800),
    )
    candidates = [_candidate("https://x.com/a"), _candidate("https://x.com/b")]
    result = matcher.match_candidates({"canonical_name": "Demo"}, candidates, client=fake)

    prices = [d["price_used"] for d in result.confirmed_matches]
    assert prices == [800.0, None]  # priced one first, priceless one last, not crashed/misordered


# --- resilience ------------------------------------------------------------------------

def test_match_candidates_skips_failing_candidate_without_crashing_batch():
    class _RaisingCompletions:
        def __init__(self):
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("simulated LLM outage")
            return _FakeResponse(_decision_json(True, 0.9, 700))

    class _RaisingChat:
        def __init__(self):
            self.completions = _RaisingCompletions()

    class _RaisingClient:
        def __init__(self):
            self.chat = _RaisingChat()

    candidates = [_candidate("https://x.com/broken"), _candidate("https://x.com/fine")]
    result = matcher.match_candidates({"canonical_name": "Demo"}, candidates, client=_RaisingClient())

    assert result.skipped == ["https://x.com/broken"]
    assert len(result.all_decisions) == 1  # only the one that didn't raise
    assert result.confirmed_matches[0]["price_used"] == 700.0


# --- accepts objects with to_dict() as well as plain dicts -----------------------------

def test_match_candidates_accepts_objects_with_to_dict():
    class _ObjCandidate:
        def __init__(self, url):
            self._url = url

        def to_dict(self):
            return _candidate(self._url)

    fake = _FakeClient(_decision_json(True, 0.9, 1000))
    result = matcher.match_candidates({"canonical_name": "Demo"}, [_ObjCandidate("https://x.com/obj")], client=fake)
    assert result.confirmed_matches[0]["candidate_url"] == "https://x.com/obj"


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
