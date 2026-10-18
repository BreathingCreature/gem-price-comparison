"""Kit 20: record the shops once, replay the same listings many times.

A replay run must never touch a shop: the wrapped functions answer from the
recording. If the pipeline asks for something the recording has no answer to
(the LLM produced a different query), that is said clearly instead of quietly
comparing different listings.
"""
import json

import pytest

from core.pipeline import run_pipeline
from eval import replay
from llm.match import MatchBatchResult
from scrapers._base import CandidateResult, SearchScrapeResult

GEM_URL = "https://mkp.gem.gov.in/p-1"


def _candidate(url="https://www.flipkart.com/x/p/itm123456", domain="flipkart.com", title="X Mouse", price=100.0):
    return CandidateResult(
        source_domain=domain,
        url=url,
        scraper_used="flipkart",
        structured_fields={"title": title, "price": price},
        raw_page_text="page text",
    )


def _search_result(query):
    return SearchScrapeResult(
        source_domain="flipkart.com", query=query, candidates=[_candidate()], blocked=False, pages_scraped=1
    )


def _shops():
    return {
        "flipkart_search_fn": _search_result,
        "amazon_search_fn": lambda q: SearchScrapeResult(
            source_domain="amazon.in", query=q, candidates=[], blocked=False, pages_scraped=1
        ),
        "discover_fn": lambda queries, allowed: [
            {
                "query": queries[0],
                "raw_urls": ["https://www.croma.com/m650"],
                "filtered_urls": ["https://www.croma.com/m650"],
                "blocked": False,
                "error": None,
            }
        ],
        "generic_scrape_fn": lambda url: CandidateResult(
            source_domain="croma.com",
            url=url,
            scraper_used="generic",
            structured_fields={"title": "M650", "price": 1399.0},
            raw_page_text="t",
        ),
    }


def _store(tmp_path, gem_url=GEM_URL):
    return replay.RecordingStore(tmp_path / "rec", gem_url)


def test_recording_saves_every_shop_answer(tmp_path):
    store = _store(tmp_path)
    wrapped = replay.wrap_shops(store, "record", **_shops())
    wrapped["flipkart_search_fn"]("gaming mouse")
    wrapped["discover_fn"](["gaming mouse"], None)
    wrapped["generic_scrape_fn"]("https://www.croma.com/m650")
    store.save()

    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["gem_url"] == GEM_URL
    values = list(saved["responses"].values())
    assert len(values) == 3
    assert _search_result("gaming mouse").to_dict() in values
    assert any(isinstance(v, list) and v[0].get("filtered_urls") == ["https://www.croma.com/m650"] for v in values)
    assert any(
        isinstance(v, dict) and v.get("url") == "https://www.croma.com/m650"
        and v.get("structured_fields", {}).get("price") == 1399.0
        for v in values
    )


def test_replay_returns_the_recorded_answers_without_touching_the_shops(tmp_path):
    store = _store(tmp_path)
    recording = replay.wrap_shops(store, "record", **_shops())
    recording["flipkart_search_fn"]("gaming mouse")
    recording["discover_fn"](["gaming mouse"], None)
    recording["generic_scrape_fn"]("https://www.croma.com/m650")
    store.save()

    def _boom(*a, **k):
        raise AssertionError("the shops must not be called during a replay")

    replays = replay.wrap_shops(
        _store(tmp_path),
        "replay",
        flipkart_search_fn=_boom,
        amazon_search_fn=_boom,
        discover_fn=_boom,
        generic_scrape_fn=_boom,
    )
    result = replays["flipkart_search_fn"]("gaming mouse")
    assert isinstance(result, SearchScrapeResult)
    assert result.to_dict() == _search_result("gaming mouse").to_dict()
    assert replays["discover_fn"](["gaming mouse"], None)[0]["filtered_urls"] == ["https://www.croma.com/m650"]
    scraped = replays["generic_scrape_fn"]("https://www.croma.com/m650")
    assert isinstance(scraped, CandidateResult)
    assert scraped.structured_fields["price"] == 1399.0


def test_recording_a_second_run_keeps_the_first_answers(tmp_path):
    # An errored product is retried on resume; the retry must not throw away
    # the answers the earlier attempt already recorded.
    first_store = _store(tmp_path)
    first = replay.wrap_shops(first_store, "record", **_shops())
    first["flipkart_search_fn"]("query one")
    first_store.save()

    second_store = _store(tmp_path)
    second = replay.wrap_shops(second_store, "record", **_shops())
    second["amazon_search_fn"]("query two")
    second_store.save()

    values = list(json.loads(second_store.path.read_text(encoding="utf-8"))["responses"].values())
    assert any(v.get("query") == "query one" for v in values)
    assert any(v.get("query") == "query two" for v in values)


def test_replay_of_an_unrecorded_query_explains_itself(tmp_path):
    store = _store(tmp_path)
    recording = replay.wrap_shops(store, "record", **_shops())
    recording["flipkart_search_fn"]("query one")
    store.save()

    replays = replay.wrap_shops(_store(tmp_path), "replay", **_shops())
    with pytest.raises(replay.ReplayDivergence) as err:
        replays["amazon_search_fn"]("never recorded")
    message = str(err.value)
    assert "amazon" in message and "never recorded" in message
    assert "record" in message.lower()


def test_make_run_fn_runs_the_pipeline_with_wrapped_shops_and_saves(tmp_path):
    store_dir = tmp_path / "rec"
    seen = {}

    def fake_pipeline(gem_url, refresh=False, on_event=None, **kwargs):
        seen.update(kwargs)
        kwargs["flipkart_search_fn"]("gaming mouse")
        kwargs["generic_scrape_fn"]("https://www.croma.com/m650")
        return {"matches": [], "trace_id": "t1"}

    run_fn = replay.make_run_fn(store_dir, "record", pipeline_fn=fake_pipeline, shops=_shops())
    out = run_fn(GEM_URL, refresh=True, on_event=None)
    assert out["trace_id"] == "t1"
    assert set(seen) == {"flipkart_search_fn", "amazon_search_fn", "discover_fn", "generic_scrape_fn"}
    files = list(store_dir.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["gem_url"] == GEM_URL
    assert len(data["responses"]) == 2


def test_replay_cli_writes_results_and_meta(tmp_path, monkeypatch):
    key = tmp_path / "key.csv"
    key.write_text("id,gem_url\nP1,https://mkp.gem.gov.in/p-1\n", encoding="utf-8")

    def fake_run_fn(gem_url, refresh=False, on_event=None):
        on_event({"type": "llm_call", "ok": True, "model": "m1"})
        return {"matches": [], "not_found_on": [], "search_issues": [], "trace_id": "t1"}

    monkeypatch.setattr(replay, "make_run_fn", lambda *a, **k: fake_run_fn)
    rc = replay.main([
        "--key", str(key), "--record", "baseline",
        "--dir", str(tmp_path / "results"), "--rec-dir", str(tmp_path / "rec"),
    ])
    assert rc == 0
    assert "P1" in (tmp_path / "results" / "baseline.jsonl").read_text(encoding="utf-8")
    meta = json.loads((tmp_path / "results" / "baseline.meta.json").read_text(encoding="utf-8"))
    assert meta["replay"] == {"mode": "record", "recording": "baseline"}


def test_replay_cli_default_label_marks_the_mode(tmp_path, monkeypatch):
    key = tmp_path / "key.csv"
    key.write_text("id,gem_url\nP1,https://mkp.gem.gov.in/p-1\n", encoding="utf-8")

    def fake_run_fn(gem_url, refresh=False, on_event=None):
        return {"matches": [], "not_found_on": [], "search_issues": [], "trace_id": "t1"}

    monkeypatch.setattr(replay, "make_run_fn", lambda *a, **k: fake_run_fn)
    assert replay.main([
        "--key", str(key), "--replay", "baseline",
        "--dir", str(tmp_path / "results"), "--rec-dir", str(tmp_path / "rec"),
    ]) == 0
    assert (tmp_path / "results" / "baseline-replay.jsonl").exists()


def test_record_or_replay_is_required(tmp_path):
    key = tmp_path / "key.csv"
    key.write_text("id,gem_url\nP1,https://mkp.gem.gov.in/p-1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        replay.main(["--key", str(key)])
    with pytest.raises(SystemExit):
        replay.main(["--key", str(key), "--record", "a", "--replay", "a"])


def test_a_replayed_run_through_the_real_pipeline_finds_the_same_matches(tmp_path):
    url = "https://mkp.gem.gov.in/test/replay/p-9-9-cat.html"

    def _boom(*a, **k):
        raise AssertionError("this stage must not run")

    def _gem(u):
        return {
            "title": "Test Mouse", "brand": "TestBrand", "model": "M1", "specs": {},
            "gem_price": 1299.0, "gem_price_type": "fixed", "category_slug": "mouse",
            "product_slug": "m1", "currency": "INR",
        }

    def _normalized(gp):
        return {
            "canonical_name": "Test Mouse M1", "brand": "TestBrand", "model": "M1",
            "key_identifiers": ["M1"], "search_queries": ["test mouse m1"],
        }

    def _match(normalized, candidates):
        decisions = []
        for c in candidates:
            d = c.to_dict() if hasattr(c, "to_dict") else c
            decisions.append({
                "candidate_url": d["url"], "source_domain": d["source_domain"], "is_match": True,
                "confidence": 0.9, "reason": "same", "extraction_source": "scraper",
                "price_used": (d.get("structured_fields") or {}).get("price"),
            })
        return MatchBatchResult(all_decisions=decisions, confirmed_matches=decisions, skipped=[])

    shops = {
        "flipkart_search_fn": lambda q: SearchScrapeResult("flipkart.com", q, [
            CandidateResult("flipkart.com", "https://www.flipkart.com/m1/p/itm1", "flipkart",
                            {"title": "Test Mouse M1", "price": 1100.0}, "")]),
        "amazon_search_fn": lambda q: SearchScrapeResult("amazon.in", q, [
            CandidateResult("amazon.in", "https://www.amazon.in/dp/B0ABCDEFGH", "amazon",
                            {"title": "Test Mouse M1", "price": 1050.0}, "")]),
        "discover_fn": lambda queries, allowed: [{
            "query": queries[0], "raw_urls": ["https://www.croma.com/m1"],
            "filtered_urls": ["https://www.croma.com/m1"], "blocked": False, "error": None,
        }],
        "generic_scrape_fn": lambda u: CandidateResult("croma.com", u, "generic",
                                                       {"title": "Test Mouse M1", "price": 1150.0}, "text"),
    }
    rec_dir = tmp_path / "recs"

    store = replay.RecordingStore(rec_dir, url)
    first = run_pipeline(
        url, refresh=True, extract_fn=_gem, normalize_fn=_normalized, match_fn=_match,
        expand_search_fn=_boom, **replay.wrap_shops(store, "record", **shops),
    )
    store.save()

    replayed = run_pipeline(
        url, refresh=True, extract_fn=_gem, normalize_fn=_normalized, match_fn=_match,
        expand_search_fn=_boom,
        **replay.wrap_shops(
            replay.RecordingStore(rec_dir, url), "replay",
            flipkart_search_fn=_boom, amazon_search_fn=_boom, discover_fn=_boom, generic_scrape_fn=_boom,
        ),
    )
    assert replayed["trace_id"] != first["trace_id"]
    assert [m["candidate_url"] for m in replayed["matches"]] == [m["candidate_url"] for m in first["matches"]]
    assert sorted(m["price_used"] for m in replayed["matches"]) == [1050.0, 1100.0, 1150.0]
    assert replayed["not_found_on"] == first["not_found_on"]
    assert replayed["comparison"] == first["comparison"]
