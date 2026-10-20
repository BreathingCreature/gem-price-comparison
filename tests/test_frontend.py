"""
Tests for frontend/app.js and frontend/render.js (Stage 8).

There is no browser here. These are source checks plus one real Node run of
render.js with a sample v2 result (the file is DOM-free on purpose).
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
RENDER_JS_PATH = FRONTEND / "render.js"
RENDER_JS = RENDER_JS_PATH.read_text(encoding="utf-8")
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")


def test_every_result_event_the_page_uses_has_a_handler():
    for event in ("shop_started", "shop_finished", "shop_updated", "retry_started",
                  "run_updated", "similar_ready", "run_failed", "done"):
        assert f'case "{event}"' in APP_JS, f"no handler for {event}"


def test_the_similar_section_needs_an_empty_match_list():
    assert "showsSimilar" in RENDER_JS
    shows = RENDER_JS.split("function showsSimilar", 1)[1][:220]
    assert "matches" in shows and "length === 0" in shows
    assert '$("similar-section").hidden' in APP_JS


def test_the_old_step_machinery_is_gone():
    assert "STEP_LABELS" not in APP_JS
    assert "PHASE_STEPS" not in APP_JS
    assert "activeSteps" not in APP_JS


def test_no_innerhtml_anywhere():
    for source in (APP_JS, RENDER_JS):
        assert "innerHTML" not in source
        assert "outerHTML" not in source


def test_safehref_only_lets_web_links_through():
    body = RENDER_JS.split("function safeHref(", 1)[1].split("}", 1)[0]
    assert "https?" in body
    assert "return null" in body
    assert "safeHref" in APP_JS


def test_every_link_in_the_page_goes_through_linkortext():
    assert APP_JS.count("linkOrText(") >= 4
    assert "safeHref" in APP_JS.split("function linkOrText", 1)[1].split("}", 1)[0]


def test_unknown_events_are_ignored_and_handlers_are_guarded():
    assert "default:" in APP_JS
    assert "unknown event types are ignored" in APP_JS
    assert "catch (e)" in APP_JS and "handle(event)" in APP_JS


def test_the_sse_connection_uses_the_done_event_and_reconnects():
    assert "Last-Event-ID" in APP_JS
    assert 'addEventListener("done"' in APP_JS


def _node():
    return shutil.which("node")


@pytest.mark.skipif(not _node(), reason="Node is not installed")
def test_render_js_formats_a_v2_result_without_a_browser(tmp_path):
    sample = {
        "result_version": 2,
        "state": "done",
        "from_cache": False,
        "checked_at": "2026-10-20T10:00:00+00:00",
        "gem": {"title": "Logitech M650", "price": {"low": 1299.0, "high": 1299.0, "kind": "fixed"}},
        "shops": {
            "flipkart.com": {"status": "found", "seen": 3, "dropped": 1, "checked": 2, "matches": 1,
                             "price": 1149.0, "retried": True, "retryable": False, "seconds": 6.2},
            "example-shop.in": {"status": "not_searched", "reason": "blocked/CAPTCHA", "seen": 0, "dropped": 0,
                                "checked": 0, "matches": 0, "retried": False, "retryable": True, "seconds": 2.0},
        },
        "matches": [
            {"shop": "flipkart.com", "title": "M650", "price": 1149.0, "confidence": 0.94,
             "reason": "same model", "url": "https://flipkart.com/x", "in_stock": True},
        ],
        "possible_matches": [{"shop": "amazon.in", "title": "M650", "price": 1249.0, "why": "confidence 0.70 below 0.75"}],
        "similar": [],
        "cheapest": {"shop": "flipkart.com", "price": 1149.0, "saving_vs_gem": 150.0},
        "warnings": [],
    }
    script = (
        "const r = require(process.argv[1]);"
        "const v = JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify({"
        "line: r.cheapestLine(v),"
        "state: r.stateLabel(v),"
        "cached: r.stateLabel(Object.assign({}, v, {from_cache: true})),"
        "rows: r.matchRows(v),"
        "possible: r.possibleRows(v),"
        "similar: r.showsSimilar(v),"
        "shop: r.shopSummary(v.shops['flipkart.com']),"
        "reason: r.shopNeedsReason(v.shops['example-shop.in']),"
        "cls: r.statusClass('not_searched'),"
        "safe: r.safeHref('javascript:alert(1)'),"
        "ok: r.safeHref('https://flipkart.com/x')"
        "}));"
    )
    proc = subprocess.run(
        [_node(), "-e", script, str(RENDER_JS_PATH), json.dumps(sample)],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["line"] == "Cheapest: \u20b91,149.00 on flipkart.com \u2014 \u20b9150.00 less than GeM"
    assert out["state"] == "complete"
    assert out["cached"].startswith("cached result from 2026-10-20")
    assert out["rows"][0]["priceText"] == "\u20b91,149.00"
    assert out["rows"][0]["vsGem"] == "\u20b9150.00 less"
    assert out["possible"][0]["why"].startswith("confidence 0.70")
    assert out["similar"] is False
    assert out["shop"].startswith("found \u00b7 3 seen")
    assert out["reason"] is True
    assert out["cls"] == "shop-unfinished"
    assert out["safe"] is None
    assert out["ok"] == "https://flipkart.com/x"


@pytest.mark.skipif(not _node(), reason="Node is not installed")
def test_an_unpriced_result_does_not_invent_a_cheapest_line(tmp_path):
    script = "const r = require(process.argv[1]); console.log(r.cheapestLine({matches: [], cheapest: null}));"
    proc = subprocess.run([_node(), "-e", script, str(RENDER_JS_PATH)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "Cheapest: none found"
