"""The live activity feed (frontend/render.js describeEvent / applyEvent).

Node runs the real file, so a wrong sentence or a wrong counter fails here.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
RENDER = FRONTEND / "render.js"
APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="Node is not installed")


def _run(expression: str, payload):
    script = (
        "const r = require(process.argv[1]); const input = JSON.parse(process.argv[2]);"
        f"console.log(JSON.stringify({expression}));"
    )
    proc = subprocess.run(
        [shutil.which("node"), "-e", script, str(RENDER), json.dumps(payload)],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# Every event type the pipeline emits (see grep emit( in core/ llm/) must have a sentence,
# except the ones that are only data for other events.
EVENT_SAMPLES = [
    {"type": "run_started"}, {"type": "cache_hit"},
    {"type": "stage", "name": "extract", "label": "Reading the GeM product page"},
    {"type": "gem_product", "title": "Mouse", "price": 100, "price_type": "fixed"},
    {"type": "normalized", "canonical_name": "Mouse", "brand": "A", "model": "B1", "queries": ["a b1"], "key_identifiers": ["B1"]},
    {"type": "llm_call", "endpoint": "e", "purpose": "verify", "seconds": 1.2, "ok": True},
    {"type": "llm_call", "endpoint": "e", "purpose": "verify", "seconds": 1.2, "ok": False, "status": 429, "error": "slow down"},
    {"type": "llm_cache_hit", "purpose": "verify"}, {"type": "llm_cooldown", "endpoint": "e", "seconds": 5},
    {"type": "llm_used_up", "endpoint": "e", "until": "00:00"}, {"type": "llm_disabled", "endpoint": "e"},
    {"type": "llm_unavailable", "reason": "all busy"},
    {"type": "shop_started", "shop": "flipkart.com"},
    {"type": "search_started", "marketplace": "flipkart", "query": "q"},
    {"type": "search_finished", "marketplace": "flipkart", "query": "q", "count": 2, "candidates": [{"title": "t", "price": 5}]},
    {"type": "discovery_finished", "queries": ["q"], "urls": ["https://x.in/a"]},
    {"type": "scrape_started", "url": "https://x.in/a"},
    {"type": "scrape_finished", "url": "https://x.in/a", "title": "t", "price": 5, "domain": "x.in"},
    {"type": "prefilter_finished", "kept": 3, "dropped": 1, "top": 10},
    {"type": "filter_finished", "shop": "amazon", "seen": 9, "dropped": 4, "sent": 5},
    {"type": "candidates_total", "count": 3},
    {"type": "candidate_checking", "url": "u", "domain": "amazon.in", "title": "t", "price": 5, "index": 1, "total": 3},
    {"type": "candidate_verdict", "url": "u", "domain": "amazon.in", "is_match": True, "confidence": 0.9, "reason": "same", "price": 5},
    {"type": "candidate_reused", "domain": "amazon.in"},
    {"type": "candidate_gate_rejected", "url": "https://amazon.in/x", "reason": "price"},
    {"type": "candidate_skipped", "url": "https://amazon.in/x", "reason": "r"},
    {"type": "lane_failed", "lane": "flipkart", "error": "boom"},
    {"type": "shop_finished", "shop": "amazon.in", "status": "found", "matches": 1, "seconds": 3},
    {"type": "run_finished", "matches": 1, "cheapest": 5},
    {"type": "retry_started", "shops": ["flipkart.com"]}, {"type": "retry_plan", "new_queries": ["q2"], "notes": "n"},
    {"type": "shop_updated", "shop": "flipkart.com", "status": "found", "matches": 1},
    {"type": "similar_ready", "similar": []}, {"type": "run_updated"}, {"type": "done"},
    {"type": "run_failed", "error": "bad page"},
]


def test_every_event_has_a_sentence_or_is_data_only():
    lines = _run("input.map(e => r.describeEvent(e))", EVENT_SAMPLES)
    for event, line in zip(EVENT_SAMPLES, lines):
        assert line is not None, f"no sentence for {event['type']}"
        assert line["text"].strip(), event["type"]
        assert line["kind"] and "key" in line


def test_unknown_events_make_no_line_and_no_crash():
    out = _run("[r.describeEvent({type: 'something_new'}), r.describeEvent(null), r.describeEvent({})]", None)
    assert out == [None, None, None]


def test_search_finished_lists_the_listings_it_found():
    line = _run("r.describeEvent(input)", {
        "type": "search_finished", "marketplace": "amazon", "query": "logitech m650", "count": 2,
        "candidates": [{"title": "Logitech M650", "price": 1199}, {"title": "Logitech M221", "price": 799}]})
    assert "amazon.in" in line["text"] and "2 listings" in line["text"]
    assert any("Logitech M650" in d and "1,199" in d for d in line["detail"])


def test_a_blocked_search_says_so():
    line = _run("r.describeEvent(input)", {"type": "search_finished", "marketplace": "flipkart", "query": "q", "count": 0, "blocked": True})
    assert line["level"] == "warn" and "blocked" in line["text"]


def test_a_verdict_shows_the_reason_and_the_confidence():
    line = _run("r.describeEvent(input)", {"type": "candidate_verdict", "domain": "www.amazon.in", "is_match": False,
                                          "confidence": 0.9, "reason": "Different model (M221).", "price": None})
    assert line["text"].startswith("Not the same on amazon.in") and "90%" in line["text"]
    assert line["detail"] == ["Different model (M221)."]


def test_the_counters_follow_one_shops_story():
    events = [
        {"type": "shop_started", "shop": "flipkart.com"},
        {"type": "search_started", "marketplace": "flipkart", "query": "a"},
        {"type": "search_finished", "marketplace": "flipkart", "query": "a", "count": 12},
        {"type": "filter_finished", "shop": "flipkart", "seen": 12, "dropped": 7, "sent": 5},
        {"type": "candidate_checking", "domain": "flipkart.com", "index": 1, "total": 5},
        {"type": "candidate_verdict", "domain": "flipkart.com", "is_match": True},
        {"type": "candidate_checking", "domain": "flipkart.com", "index": 2, "total": 5},
        {"type": "candidate_verdict", "domain": "flipkart.com", "is_match": False},
        {"type": "llm_call", "ok": True, "seconds": 2}, {"type": "llm_call", "ok": False, "seconds": 1},
    ]
    stats = _run("(() => { const s = r.newStats(); input.forEach(e => r.applyEvent(s, e)); return s; })()", events)
    shop = stats["shops"]["flipkart.com"]
    assert (shop["queries"], shop["queriesDone"], shop["listings"]) == (1, 1, 12)
    assert (shop["dropped"], shop["kept"], shop["judged"], shop["matches"], shop["rejected"]) == (7, 5, 2, 1, 1)
    assert stats["llm"]["calls"] == 2 and stats["llm"]["failed"] == 1
    line = _run("r.liveShopLine(input)", shop)
    assert "1/1 searches" in line and "12 listings" in line and "2/2 checked" in line


def test_the_progress_strip_only_moves_forward():
    stats = _run(
        "(() => { const s = r.newStats(); input.forEach(e => r.applyEvent(s, e)); return s; })()",
        [{"type": "stage", "name": "extract"}, {"type": "stage", "name": "normalize"},
         {"type": "stage", "name": "search_both"}, {"type": "stage", "name": "match_amazon"},
         {"type": "stage", "name": "search_flipkart"}])  # a late search stage must not move it back
    assert stats["step"] == "verify"
    assert stats["doneSteps"]["read"] and stats["doneSteps"]["search"]


def test_shop_names_map_to_one_card():
    names = _run("['flipkart','Amazon','https://www.amazon.in/dp/x','other_shops','shop.in'].map(r.shopKey)", None)
    assert names == ["flipkart.com", "amazon.in", "amazon.in", "other", "shop.in"]


def test_the_page_replays_a_saved_runs_events_and_keeps_text_safe():
    assert "replayTrace" in APP_JS and "/api/traces/" in APP_JS
    assert "R.describeEvent" in APP_JS
    assert "innerHTML" not in APP_JS


def test_the_clock_formats_minutes_and_seconds():
    assert _run("[r.clock(0), r.clock(59.9), r.clock(75), r.clock(3600)]", None) == ["00:00", "00:59", "01:15", "60:00"]
