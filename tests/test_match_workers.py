"""GEM_LLM_WORKERS: several listings checked at once, answers kept in order."""
import threading
import time

from llm import match as matcher


def _candidate(url, price=1000.0):
    return {
        "url": url,
        "source_domain": "shop.example",
        "structured_fields": {"title": "Demo " + url[-1], "price": price, "currency": "INR"},
        "raw_page_text": "",
    }


def _decision(candidate, is_match=True, confidence=0.9):
    return {
        "candidate_url": candidate["url"],
        "source_domain": candidate["source_domain"],
        "is_match": is_match,
        "confidence": confidence,
        "reason": "looks the same",
        "price_used": 900.0 if is_match else None,
        "extraction_source": "scraper",
    }


def _three():
    return [
        _candidate("https://shop.example/a"),
        _candidate("https://shop.example/b"),
        _candidate("https://shop.example/c"),
    ]


def test_default_is_one_at_a_time_in_this_thread(monkeypatch):
    monkeypatch.setattr(matcher, "LLM_WORKERS", 1, raising=False)
    threads = []

    def fake(normalized, candidate, client=None):
        threads.append(threading.current_thread().name)
        return _decision(candidate)

    monkeypatch.setattr(matcher, "verify_or_extract", fake)
    result = matcher.match_candidates({"gem_price": 1000.0}, _three())
    assert len(result.confirmed_matches) == 3
    assert threads == [threading.current_thread().name] * 3


def test_several_workers_verify_at_the_same_time(monkeypatch):
    monkeypatch.setattr(matcher, "LLM_WORKERS", 3, raising=False)
    gate = threading.Barrier(3, timeout=5)

    def fake(normalized, candidate, client=None):
        gate.wait()  # only passes if all three calls are running at once
        return _decision(candidate)

    monkeypatch.setattr(matcher, "verify_or_extract", fake)
    result = matcher.match_candidates({"gem_price": 1000.0}, _three())
    assert len(result.confirmed_matches) == 3


def test_more_workers_than_listings_is_fine(monkeypatch):
    monkeypatch.setattr(matcher, "LLM_WORKERS", 10, raising=False)
    monkeypatch.setattr(matcher, "verify_or_extract", lambda n, c, client=None: _decision(c), raising=False)
    result = matcher.match_candidates({"gem_price": 1000.0}, _three())
    assert len(result.confirmed_matches) == 3


def test_answers_keep_the_candidate_order(monkeypatch):
    monkeypatch.setattr(matcher, "LLM_WORKERS", 3, raising=False)

    def fake(normalized, candidate, client=None):
        if candidate["url"].endswith("/a"):
            time.sleep(0.05)  # the slow one — the others finish first
        return _decision(candidate)

    monkeypatch.setattr(matcher, "verify_or_extract", fake)
    result = matcher.match_candidates({"gem_price": 1000.0}, _three())
    assert [d["candidate_url"] for d in result.all_decisions] == [
        "https://shop.example/a",
        "https://shop.example/b",
        "https://shop.example/c",
    ]


def test_one_failed_call_does_not_kill_the_others(monkeypatch):
    monkeypatch.setattr(matcher, "LLM_WORKERS", 3, raising=False)

    def fake(normalized, candidate, client=None):
        if candidate["url"].endswith("/b"):
            raise RuntimeError("model fell over")
        return _decision(candidate)

    monkeypatch.setattr(matcher, "verify_or_extract", fake)
    result = matcher.match_candidates({"gem_price": 1000.0}, _three())
    assert [d["candidate_url"] for d in result.confirmed_matches] == [
        "https://shop.example/a",
        "https://shop.example/c",
    ]
    assert result.skipped == ["https://shop.example/b"]


def test_a_failed_fetch_never_reaches_the_workers(monkeypatch):
    monkeypatch.setattr(matcher, "LLM_WORKERS", 3, raising=False)
    calls = []

    def fake(normalized, candidate, client=None):
        calls.append(candidate["url"])
        return _decision(candidate)

    monkeypatch.setattr(matcher, "verify_or_extract", fake)
    broken = _candidate("https://shop.example/x")
    broken["fetch_error"] = "timeout"
    broken["structured_fields"] = None
    result = matcher.match_candidates({"gem_price": 1000.0}, [broken, _candidate("https://shop.example/a")])
    assert calls == ["https://shop.example/a"]
    assert result.skipped == ["https://shop.example/x"]
