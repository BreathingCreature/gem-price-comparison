"""The matcher reports each listing it checks and what it decided."""
from types import SimpleNamespace

from core import events
from llm import client as llm_client
from llm.match import match_candidates


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        text = self.answers.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _verdict(is_match, price, confidence=0.9):
    return (
        '{"is_match": %s, "confidence": %s, "reason": "because", "extracted_title": "T", '
        '"extracted_price": %s, "extraction_source": "scraper"}'
    ) % (str(is_match).lower(), confidence, price)


def _candidate(url, price=1000.0, currency="INR", fetch_error=None, with_fields=True):
    return {
        "url": url,
        "source_domain": "shop.example",
        "structured_fields": {"title": "Demo " + url[-1], "price": price, "currency": currency} if with_fields else None,
        "raw_page_text": "",
        "fetch_error": fetch_error,
    }


def _run(candidates, client, gem_price=1000.0, monkeypatch=None):
    seen = []
    with events.capture(seen.append):
        result = match_candidates({"canonical_name": "Demo", "gem_price": gem_price}, candidates, client=client)
    return result, seen


def _of(seen, type_):
    return [e for e in seen if e["type"] == type_]


def test_each_candidate_is_announced_then_judged(monkeypatch):
    monkeypatch.setattr(llm_client, "NVIDIA_MIN_SECONDS_BETWEEN_CALLS", 0.0)
    client = FakeClient(_verdict(True, 900), _verdict(False, 500))
    _, seen = _run([_candidate("https://shop.example/a"), _candidate("https://shop.example/b")], client)

    checking = _of(seen, "candidate_checking")
    assert [(e["index"], e["total"]) for e in checking] == [(1, 2), (2, 2)]
    assert checking[0]["title"] == "Demo a" and checking[0]["price"] == 1000.0

    verdicts = _of(seen, "candidate_verdict")
    assert [e["is_match"] for e in verdicts] == [True, False]
    assert verdicts[0]["confidence"] == 0.9 and verdicts[0]["reason"] == "because"
    assert verdicts[0]["url"] == "https://shop.example/a"


def test_a_foreign_currency_is_reported_as_a_rejection(monkeypatch):
    monkeypatch.setattr(llm_client, "NVIDIA_MIN_SECONDS_BETWEEN_CALLS", 0.0)
    _, seen = _run([_candidate("https://shop.example/a", currency="USD")], FakeClient())

    verdict = _of(seen, "candidate_verdict")[0]
    assert verdict["is_match"] is False
    assert "currency" in verdict["reason"]


def test_a_failed_fetch_is_reported_as_skipped(monkeypatch):
    monkeypatch.setattr(llm_client, "NVIDIA_MIN_SECONDS_BETWEEN_CALLS", 0.0)
    broken = _candidate("https://shop.example/a", fetch_error="timeout", with_fields=False)
    _, seen = _run([broken], FakeClient())

    skipped = _of(seen, "candidate_skipped")
    assert len(skipped) == 1 and "timeout" in skipped[0]["reason"]


def test_an_llm_failure_is_reported_as_skipped(monkeypatch):
    monkeypatch.setattr(llm_client, "NVIDIA_MIN_SECONDS_BETWEEN_CALLS", 0.0)

    class Broken:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs):
            raise RuntimeError("model fell over")

    _, seen = _run([_candidate("https://shop.example/a")], Broken())
    skipped = _of(seen, "candidate_skipped")
    assert len(skipped) == 1 and "model fell over" in skipped[0]["reason"]


def test_the_price_safety_check_reports_its_rejection(monkeypatch):
    monkeypatch.setattr(llm_client, "NVIDIA_MIN_SECONDS_BETWEEN_CALLS", 0.0)
    client = FakeClient(_verdict(True, 9000))  # 9x the GeM price
    result, seen = _run([_candidate("https://shop.example/a", price=9000.0)], client, gem_price=1000.0)

    assert result.confirmed_matches == []
    rejected = _of(seen, "candidate_gate_rejected")
    assert len(rejected) == 1
    assert rejected[0]["url"] == "https://shop.example/a"
    assert "outside" in rejected[0]["reason"]


def test_matching_without_a_listener_is_unchanged(monkeypatch):
    monkeypatch.setattr(llm_client, "NVIDIA_MIN_SECONDS_BETWEEN_CALLS", 0.0)
    client = FakeClient(_verdict(True, 900))
    result = match_candidates({"canonical_name": "Demo", "gem_price": 1000.0}, [_candidate("https://shop.example/a")], client=client)

    assert len(result.confirmed_matches) == 1


def test_a_lane_reports_its_filter_result_for_the_page():
    """Each lane says how many listings the filter saw, dropped and sent on (the page's shop counters)."""
    from core import events, pipeline
    from llm import match as matcher

    cands = [
        {"url": f"https://www.flipkart.com/p{i}", "source_domain": "flipkart.com",
         "structured_fields": {"title": "Logitech M650 Wireless Mouse", "price": 1000.0 + i}}
        for i in range(3)
    ]
    normalized = {"canonical_name": "Logitech M650 Wireless Mouse", "brand": "Logitech", "model": "M650", "search_queries": ["logitech m650"]}
    with events.capture() as sink:
        pipeline._verify_lane(normalized, cands, lambda n, c, **k: matcher.MatchBatchResult(), name="flipkart", label="the Flipkart listings")
    found = [e for e in sink.events if e["type"] == "filter_finished"]
    assert len(found) == 1
    assert found[0]["shop"] == "flipkart" and found[0]["seen"] == 3 and found[0]["sent"] + found[0]["dropped"] == 3
