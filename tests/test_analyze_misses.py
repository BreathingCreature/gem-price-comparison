"""Every real match that was missed gets a reason (eval/analyze_misses.py).

Reasons are read from the trace, not guessed: was the listing ever returned
by a search, was it cut by the prefilter, did the LLM say no, did the safety
gate reject it. The safe prefilter N is then chosen from the rank data.
"""
import json

from eval import analyze_misses
from eval.score import load_key, load_results

KEY_HEADER = "id,gem_url,gem_name,branded,gem_price,amazon_url,amazon_price,flipkart_url,flipkart_price,other_urls,notes"
AMAZON_URL = "https://www.amazon.in/dp/B0ABCDEFGH"


def _write_key(tmp_path, amazon_url=AMAZON_URL, flipkart="NONE"):
    path = tmp_path / "key.csv"
    path.write_text(
        KEY_HEADER + "\n"
        f"P1,https://mkp.gem.gov.in/p-1,Test Mouse,no,1299,{amazon_url},1500,{flipkart},,,\n",
        encoding="utf-8",
    )
    return path


def _write_results(tmp_path, matches=(), trace_id="t1"):
    path = tmp_path / "results.jsonl"
    rec = {
        "id": "P1",
        "gem_url": "https://mkp.gem.gov.in/p-1",
        "seconds": 1.0,
        "error": None,
        "trace_id": trace_id,
        "matches": list(matches),
        "not_found_on": [],
        "search_issues": [],
        "llm_calls": 0,
        "llm_failures": 0,
        "cache_hits": 0,
        "llm_models": {},
    }
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return path


def _write_trace(tmp_path, stages, trace_id="t1", gem_url="https://mkp.gem.gov.in/p-1"):
    traces = tmp_path / "traces"
    traces.mkdir(exist_ok=True)
    (traces / f"{trace_id}.json").write_text(
        json.dumps({"trace_id": trace_id, "gem_url": gem_url, "stages": stages, "events": []}),
        encoding="utf-8",
    )
    return traces


def _candidate(url=AMAZON_URL, title="Test Mouse", price=1500.0, fetch_error=None):
    return {
        "source_domain": "amazon.in",
        "url": url,
        "scraper_used": "amazon",
        "structured_fields": {"title": title, "price": price} if fetch_error is None else None,
        "raw_page_text": "text" if fetch_error is None else None,
        "fetch_error": fetch_error,
    }


def _decision(url=AMAZON_URL, is_match=False, confidence=0.3, reason="accessory, not the product", price=1500.0):
    return {
        "candidate_url": url,
        "source_domain": "amazon.in",
        "is_match": is_match,
        "confidence": confidence,
        "reason": reason,
        "price_used": price,
        "extraction_source": "scraper",
    }


def _stages(candidate=None, decision=None, dropped=None, skipped=(), top=None):
    stages = {
        "amazon_search": [
            {"source_domain": "amazon.in", "query": "q", "candidates": [candidate] if candidate else [],
             "blocked": False, "pages_scraped": 1, "error": None}
        ],
        "flipkart_search": [],
        "discovery": [],
        "generic_scrapes": [],
        "matching": {
            "all_decisions": [decision] if decision else [],
            "confirmed_matches": [],
            "skipped": list(skipped),
        },
    }
    if top is not None:
        stages["prefilter"] = {"top": top, "dropped": dropped or []}
    return stages


def _analyze(tmp_path, key, results, traces):
    return analyze_misses.analyze(load_key(key), load_results(results), traces)


def test_a_listing_the_search_never_returned_is_never_found(tmp_path):
    key = _write_key(tmp_path)
    results = _write_results(tmp_path)
    traces = _write_trace(tmp_path, _stages())

    out = _analyze(tmp_path, key, results, traces)
    assert out["counts"] == {"never_found": 1}
    miss = out["misses"][0]
    assert miss["id"] == "P1" and miss["site"] == "amazon" and miss["reason"] == "never_found"
    assert out["slots"] == {"wanted": 1, "found": 0}


def test_a_listing_cut_by_the_prefilter_reports_its_rank_and_the_safe_n(tmp_path):
    key = _write_key(tmp_path)
    results = _write_results(tmp_path)
    other = {"url": "https://www.amazon.in/dp/B0ZZZZZZZZZ", "source_domain": "amazon.in", "title": "Other", "score": 0.2}
    target = {"url": AMAZON_URL, "source_domain": "amazon.in", "title": "Test Mouse", "score": 0.1}
    traces = _write_trace(tmp_path, _stages(candidate=_candidate(), dropped=[other, target], top=5))

    out = _analyze(tmp_path, key, results, traces)
    miss = out["misses"][0]
    assert miss["reason"] == "cut_by_filter"
    assert "rank 7" in miss["detail"] and "GEM_PREFILTER_TOP=5" in miss["detail"]
    # rank 7: safe only from N=10 on; "off" keeps it too
    assert out["safe_n"]["5"] == 0 and out["safe_n"]["10"] == 1 and out["safe_n"]["off"] == 1


def test_llm_saying_no_is_reported_with_the_reason(tmp_path):
    key = _write_key(tmp_path)
    results = _write_results(tmp_path)
    traces = _write_trace(tmp_path, _stages(candidate=_candidate(), decision=_decision()))

    out = _analyze(tmp_path, key, results, traces)
    miss = out["misses"][0]
    assert miss["reason"] == "llm_said_no" and "accessory" in miss["detail"]


def test_a_safety_gate_rejection_is_its_own_reason(tmp_path):
    key = _write_key(tmp_path)
    results = _write_results(tmp_path)
    decision = _decision(is_match=False, confidence=0.0, reason="[hard-rejected: price 99.0 outside 0.5-3.0x GeM 1299.0]")
    traces = _write_trace(tmp_path, _stages(candidate=_candidate(), decision=decision))

    out = _analyze(tmp_path, key, results, traces)
    miss = out["misses"][0]
    assert miss["reason"] == "safety_gate" and "hard-rejected" in miss["detail"]


def test_a_failed_page_fetch_is_reported(tmp_path):
    key = _write_key(tmp_path)
    results = _write_results(tmp_path)
    traces = _write_trace(tmp_path, _stages(candidate=_candidate(fetch_error="ReadTimeout: the page timed out")))

    out = _analyze(tmp_path, key, results, traces)
    miss = out["misses"][0]
    assert miss["reason"] == "fetch_failed" and "timed out" in miss["detail"]


def test_a_verify_that_never_finished_is_reported(tmp_path):
    key = _write_key(tmp_path)
    results = _write_results(tmp_path)
    traces = _write_trace(tmp_path, _stages(candidate=_candidate(), skipped=[AMAZON_URL]))

    out = _analyze(tmp_path, key, results, traces)
    assert out["misses"][0]["reason"] == "verify_incomplete"


def test_a_match_the_llm_was_not_sure_about_is_reported_as_below_threshold(tmp_path):
    key = _write_key(tmp_path)
    results = _write_results(tmp_path)
    traces = _write_trace(tmp_path, _stages(candidate=_candidate(), decision=_decision(is_match=True, confidence=0.5)))

    out = _analyze(tmp_path, key, results, traces)
    assert out["misses"][0]["reason"] == "below_threshold"


def test_a_match_without_a_price_is_reported_as_demoted(tmp_path):
    key = _write_key(tmp_path)
    results = _write_results(tmp_path)
    decision = _decision(is_match=True, confidence=0.9, price=None)
    traces = _write_trace(tmp_path, _stages(candidate=_candidate(), decision=decision, skipped=[AMAZON_URL]))

    out = _analyze(tmp_path, key, results, traces)
    assert out["misses"][0]["reason"] == "no_price_demoted"


def test_a_found_slot_is_not_reported_as_a_miss(tmp_path):
    key = _write_key(tmp_path)
    results = _write_results(tmp_path, matches=[{"url": AMAZON_URL, "domain": "amazon.in", "price": 1500.0, "confidence": 0.9}])
    traces = _write_trace(tmp_path, _stages())

    out = _analyze(tmp_path, key, results, traces)
    assert out["misses"] == [] and out["slots"] == {"wanted": 1, "found": 1}


def test_a_missing_trace_file_says_so(tmp_path):
    key = _write_key(tmp_path)
    results = _write_results(tmp_path, trace_id="gone")
    traces = tmp_path / "traces"  # conftest created it, it stays empty

    out = _analyze(tmp_path, key, results, traces)
    assert out["misses"][0]["reason"] == "no_trace"


def test_cli_prints_the_report_and_can_write_json(tmp_path, capsys):
    key = _write_key(tmp_path)
    results = _write_results(tmp_path)
    other = {"url": "https://www.amazon.in/dp/B0ZZZZZZZZZ", "source_domain": "amazon.in", "title": "Other", "score": 0.2}
    target = {"url": AMAZON_URL, "source_domain": "amazon.in", "title": "Test Mouse", "score": 0.1}
    traces = _write_trace(tmp_path, _stages(candidate=_candidate(), dropped=[other, target], top=5))
    out_file = tmp_path / "report.json"

    rc = analyze_misses.main([
        "--key", str(key), "--results", str(results), "--traces", str(traces), "--json", str(out_file),
    ])
    assert rc == 0
    text = capsys.readouterr().out
    assert "cut by filter" in text and "P1" in text
    assert "safe prefilter n" in text.lower()
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert saved["counts"] == {"cut_by_filter": 1}


def test_a_trace_without_stages_is_read_as_empty_not_a_crash():
    # A trace file can exist without a stages block (older runs, partial write).
    trace = {"trace_id": "t1", "stages": None}

    assert analyze_misses.dropped_ranks(trace) == {}
    assert analyze_misses.candidate_entries(trace) == []
    assert analyze_misses.decisions_by_key(trace) == {}
    assert analyze_misses.discovery_urls(trace) == []
