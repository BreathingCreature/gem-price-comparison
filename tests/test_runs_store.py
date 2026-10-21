"""The small on-disk index of finished runs."""
import json

import pytest

from core import pipeline, runs
from llm.match import MatchBatchResult
from scrapers._base import SearchScrapeResult


def _empty_search(domain):
    def search(query):
        return SearchScrapeResult(source_domain=domain, query=query, candidates=[])

    return search


def _successful_run(url):
    return pipeline.run_pipeline(
        url,
        refresh=True,
        extract_fn=lambda _url: {"title": "Mouse", "gem_price": 1000.0},
        normalize_fn=lambda _product: {
            "canonical_name": "Mouse",
            "brand": "Logitech",
            "model": "M650",
            "search_queries": ["logitech m650"],
            "key_identifiers": [],
        },
        flipkart_search_fn=_empty_search("flipkart.com"),
        amazon_search_fn=_empty_search("amazon.in"),
        discover_fn=lambda queries, domains: [],
        generic_scrape_fn=lambda _url: None,
        match_fn=lambda normalized, candidates: MatchBatchResult(),
        expand_search_fn=lambda normalized, queries, domains: {
            "new_queries": [],
            "suggested_domains": [],
            "notes": "",
        },
    )


def test_a_finished_run_is_written_and_listed():
    result = _successful_run("https://mkp.gem.gov.in/test/runs/p-1-1-cat.html")
    records = runs.list_runs()
    assert records, "the run should be listed"
    newest = records[0]
    assert newest["run_id"] == result["trace_id"]
    assert newest["state"] == "done"
    assert newest["title"] == "Mouse"
    assert newest["match_count"] == 0
    assert (runs.runs_dir() / f"{result['trace_id']}.json").exists()


def test_list_is_newest_first_and_respects_the_limit():
    for index in range(3):
        runs.save_run({"run_id": f"r{index}", "saved_at": 100 + index})
    assert [r["run_id"] for r in runs.list_runs(2)] == ["r2", "r1"]
    assert len(runs.list_runs()) == 3


def test_a_damaged_record_is_skipped():
    runs.save_run({"run_id": "good", "saved_at": 1})
    (runs.runs_dir() / "broken.json").write_text("{not json", encoding="utf-8")
    assert [r["run_id"] for r in runs.list_runs()] == ["good"]


def test_a_record_that_is_not_utf8_is_skipped():
    runs.save_run({"run_id": "good", "saved_at": 1})
    (runs.runs_dir() / "bad.json").write_bytes(b'{"run_id": "\xff"}')
    assert [r["run_id"] for r in runs.list_runs()] == ["good"]


def test_a_record_with_a_string_timestamp_does_not_crash_the_list():
    # An older/hand-edited record can hold a string where a timestamp belongs;
    # sorting used to compare str with float and raise TypeError.
    runs.save_run({"run_id": "typed", "saved_at": "not-a-time"})
    runs.save_run({"run_id": "numeric", "saved_at": 5})
    assert [r["run_id"] for r in runs.list_runs()] == ["numeric", "typed"]


def test_a_nan_timestamp_does_not_break_the_order():
    runs.save_run({"run_id": "nan", "saved_at": float("nan")})
    runs.save_run({"run_id": "numeric", "saved_at": 5})
    assert [r["run_id"] for r in runs.list_runs()] == ["numeric", "nan"]


def test_a_key_inside_a_record_is_hidden():
    path = runs.save_run({"run_id": "s1", "note": "rejected nvapi-abc123DEF456"})
    text = path.read_text(encoding="utf-8")
    assert "nvapi-" not in text
    assert "[hidden]" in text


def test_a_record_needs_a_run_id():
    with pytest.raises(ValueError):
        runs.save_run({"title": "no id"})


def test_summarize_keeps_only_the_list_fields():
    summary = runs.summarize({"run_id": "r1", "title": "Mouse", "saved_at": 1, "secret": "x"})
    assert summary == {"run_id": "r1", "title": "Mouse"}
