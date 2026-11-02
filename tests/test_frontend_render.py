"""The pure front-end models in render.js, run by Node without a browser.

These cover the numbers the page shows: money, savings, bars, counters, state
labels, CSV and the copied summary. A wrong number here is a wrong page.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
RENDER = FRONTEND / "render.js"

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="Node is not installed")


def _js(expression: str):
    script = "const r = require(process.argv[1]); console.log(JSON.stringify(" + expression + "));"
    proc = subprocess.run(
        [shutil.which("node"), "-e", script, str(RENDER)],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _result():
    return {
        "gem": {"title": "Logitech M650", "price": {"low": 1299.0, "high": 1299.0, "kind": "fixed"}},
        "cheapest": {
            "shop": "flipkart.com", "title": "M650", "price": 1149.0,
            "url": "https://www.flipkart.com/x", "saving_vs_gem": 150.0, "bulk": False,
        },
        "matches": [
            {"shop": "flipkart.com", "title": "M650, wireless", "price": 1149.0, "confidence": 0.94,
             "reason": "same model", "url": "https://www.flipkart.com/x", "in_stock": True, "bulk": False},
            {"shop": "amazon.in", "title": "M650 Signature", "price": 1199.0, "confidence": 0.92,
             "reason": "same model", "url": "https://www.amazon.in/x", "in_stock": True, "bulk": False},
            {"shop": "indiamart.com", "title": "M650 bulk", "price": 1249.0, "confidence": 0.9,
             "reason": "same model", "url": "https://www.indiamart.com/x", "in_stock": True, "bulk": True},
        ],
        "possible_matches": [
            {"shop": "amazon.in", "title": "M650 pack of 2", "price": 2199.0, "confidence": 0.62,
             "why": "different pack", "url": "https://www.amazon.in/pack"},
        ],
        "warnings": ["indiamart.com is a B2B/bulk marketplace - its price may be wholesale, not retail."],
    }


def test_money_has_no_decimals_for_whole_rupees():
    assert _js("[r.money(1149), r.money(1149.5), r.money(0), r.money(null)]") == [
        "\u20b91,149", "\u20b91,149.50", "\u20b90", "-",
    ]


def test_savings_and_percent_never_invent_a_cheaper_price():
    cheaper = _js("r.savingInfo({gem:{price:{low:1299}},cheapest:{price:1149}})")
    dearer = _js("r.savingInfo({gem:{price:{low:1299}},cheapest:{price:1499}})")
    same = _js("r.savingInfo({gem:{price:{low:1299}},cheapest:{price:1299}})")
    assert cheaper["text"] == "\u20b9150 less (11.5%)" and cheaper["cheaper"] is True
    assert dearer["text"] == "\u20b9200 more (15.4%)" and dearer["dearer"] is True
    assert same["text"] == "the same price"
    assert _js("r.savingInfo({gem:{price:{low:1299}},cheapest:null})") is None
    assert _js("r.percent(0.945)") == "95%"


def test_bars_scale_to_the_gem_price_and_crown_only_a_matching_cheapest():
    bars = _js("r.barRows(" + json.dumps(_result()) + ")")
    assert bars["max"] == 1299.0
    assert [(row["label"], row["width"]) for row in bars["rows"]] == [
        ("GeM", 100), ("flipkart.com", 88), ("amazon.in", 92), ("indiamart.com", 96),
    ]
    assert [row["cheapest"] for row in bars["rows"]] == [False, True, False, False]
    assert bars["rows"][3]["bulk"] is True


def test_status_words_and_classes_cover_every_shop_state():
    out = _js(
        "['found','possible','not_found','not_searched','incomplete','timed_out','other']"
        ".map(s => [r.shopStatusLabel(s), r.statusClass(s), r.shopNeedsReason({status:s})])"
    )
    assert out[0] == ["Found a match", "status-found", False]
    assert out[1] == ["Possible only", "status-possible", False]
    assert out[2] == ["Not found", "status-not-found", True]
    assert out[3] == ["Could not search", "status-warning", True]
    assert out[4] == ["Incomplete", "status-warning", True]
    assert out[5] == ["Timed out", "status-warning", True]
    assert out[6] == ["Waiting", "status-waiting", True]


def test_the_lane_model_counts_one_shops_story_and_ignores_the_global_filter():
    stats = _js(
        "(() => { const s = r.newStats(); ["
        "{type:'shop_started',shop:'flipkart.com'},"
        "{type:'search_started',marketplace:'flipkart'},"
        "{type:'search_finished',marketplace:'flipkart',count:3},"
        "{type:'search_finished',marketplace:'flipkart',query:'q2',count:2},"
        "{type:'filter_finished',shop:'flipkart.com',seen:5,dropped:2,sent:3},"
        "{type:'candidates_total',count:3},"
        "{type:'candidate_checking',url:'u1',domain:'flipkart.com',index:1,total:3},"
        "{type:'candidate_verdict',url:'u1',domain:'flipkart.com',is_match:true,price:100},"
        "{type:'candidate_checking',url:'u2',domain:'flipkart.com',index:2,total:3},"
        "{type:'candidate_verdict',url:'u2',domain:'flipkart.com',is_match:true,price:null},"
        "{type:'candidate_checking',url:'u3',domain:'flipkart.com',index:3,total:3},"
        "{type:'candidate_verdict',url:'u3',domain:'flipkart.com',is_match:false,confidence:0.9,outcome:'no_match',price:90},"
        "{type:'candidate_gate_rejected',url:'u4',domain:'flipkart.com',reason:'price'}"
        "].forEach(e => r.applyEvent(s, e)); return s; })()"
    )
    lane = _js("r.pipelineModel(" + json.dumps(stats) + ").lanes[0]")
    assert lane["id"] == "flipkart"
    assert lane["nodes"][0]["value"] == "2/1 searches"
    assert lane["nodes"][1]["value"] == "3 kept, 2 dropped"
    assert lane["nodes"][2]["value"] == "1 match, 1 possible, 1 rejected"
    assert lane["nodes"][3]["value"] == "1 rejected"
    assert lane["matches"] == 1 and lane["possible"] == 1


def test_the_other_lane_adds_every_non_marketplace_shop():
    stats = _js(
        "(() => { const s = r.newStats(); ["
        "{type:'shop_finished',shop:'indiamart.com',status:'found',matches:1},"
        "{type:'candidate_checking',url:'u',domain:'indiamart.com',index:1,total:1},"
        "{type:'candidate_verdict',url:'u',domain:'indiamart.com',is_match:true,price:1249},"
        "{type:'shop_finished',shop:'example-shop.in',status:'not_found'},"
        "{type:'shop_finished',shop:'other',status:'not_found'}"
        "].forEach(e => r.applyEvent(s, e)); return s; })()"
    )
    lane = _js("r.pipelineModel(" + json.dumps(stats) + ").lanes[2]")
    assert lane["status"] == "found"
    assert lane["matches"] == 1


def test_answer_states_are_honest_and_never_call_a_missing_price_zero():
    empty = _js("r.answerModel(null,{active:true})")
    possible = _js("r.answerModel({gem:{title:'X'},matches:[],cheapest:null,possible_matches:[{shop:'a'}]},{finished:true})")
    incomplete = _js("r.answerModel({gem:{title:'X'},matches:[],cheapest:null,shops:{'a.com':{status:'not_searched'}}},{finished:true})")
    similar = _js("r.answerModel({gem:{title:'X'},matches:[],cheapest:null,similar:[{title:'Y'}]},{finished:true})")
    none = _js("r.answerModel({gem:{title:'X'},matches:[],cheapest:null,shops:{'a.com':{status:'not_found'}}},{finished:true})")
    assert empty["kind"] == "searching" and empty["price"] is None and empty["headline"] == "Searching"
    assert possible["kind"] == "possible" and possible["headline"] == "Not confirmed"
    assert incomplete["kind"] == "incomplete" and incomplete["headline"] == "Incomplete"
    assert similar["kind"] == "similar" and similar["headline"] == "Similar only"
    assert none["kind"] == "none" and none["headline"] == "No match"


def test_csv_and_summary_keep_the_same_facts_and_quote_a_comma():
    result = _result()
    csv = _js("r.csvText(" + json.dumps(result) + ")")
    lines = csv.splitlines()
    assert lines[0] == "shop,title,price,difference_vs_gem,confidence,in_stock,b2b,url"
    assert lines[1].startswith('flipkart.com,"M650, wireless",1149,\u20b9150 less,94%,yes,no,')
    assert lines[3].endswith(",yes,https://www.indiamart.com/x")
    summary = _js("r.summaryText(" + json.dumps(result) + ")")
    assert "Product: Logitech M650" in summary
    assert "GeM price: \u20b91,299" in summary
    assert "Best price: \u20b91,149 on flipkart.com" in summary
    assert "Saving: \u20b9150 less (11.5%)" in summary
    assert "- amazon.in: \u20b92,199, different pack" in summary
    assert "Warnings:" in summary


def test_relative_time_and_recent_state_words():
    out = _js(
        "(() => { const now = Date.parse('2026-11-01T12:00:00Z'); return {"
        "a: r.relativeTime('2026-11-01T11:59:40Z', now),"
        "b: r.relativeTime('2026-11-01T11:55:00Z', now),"
        "c: r.relativeTime('2026-11-01T08:00:00Z', now),"
        "d: r.relativeTime('2026-10-29T12:00:00Z', now),"
        "e: r.relativeTime('2026-10-01T12:00:00Z', now),"
        "run: r.recentState({live:true,state:'running'}),"
        "failed: r.recentState({state:'failed'}),"
        "cached: r.recentState({state:'done',from_cache:true}),"
        "done: r.recentState({state:'done'})}; })()"
    )
    assert out == {
        "a": "just now", "b": "5 min ago", "c": "4 hr ago", "d": "3 days ago",
        "e": "2026-10-01", "run": "running", "failed": "failed",
        "cached": "cached", "done": "done",
    }


def test_a_thousand_events_replay_quickly():
    out = _js(
        "(() => { const s = r.newStats(); const start = process.hrtime.bigint();"
        "for (let i = 0; i < 1000; i++) r.applyEvent(s, {type: i % 2 ? 'search_finished' : 'shop_started',"
        "shop:'flipkart.com', marketplace:'flipkart', count: 1});"
        "const ms = Number(process.hrtime.bigint() - start) / 1e6;"
        "return {ms, shops: Object.keys(s.shops).length}; })()"
    )
    assert out["shops"] == 1
    assert out["ms"] < 300, f"1000 events took {out['ms']:.1f} ms"

def test_is_product_url_accepts_and_rejects():
    assert _js("r.isProductUrl('https://mkp.gem.gov.in/computer-mouse/logitech-m650/p-5812034-2-cat.html')") is True
    assert _js("r.isProductUrl('https://mkp.gem.gov.in/search?q=mouse')") is False
    assert _js("r.isProductUrl('http://GEM.GOV.IN/a/b/p-1-2-cat.html?q=1')") is True
    assert _js("r.isProductUrl('javascript:alert(1)')") is False
    assert _js("r.isProductUrl('')") is False


def test_is_product_url_matches_the_python_table():
    cases = json.loads((Path(__file__).parent / "url_cases.json").read_text(encoding="utf-8"))
    urls = [case["url"] for case in cases]
    out = _js("(" + json.dumps(urls) + ").map(function (u) { return r.isProductUrl(u); })")
    assert out == [case["ok"] for case in cases]


def test_scale_for_uses_25_percent_steps():
    rows = [{"delta": 150, "price": 1149, "isGem": False}, {"delta": 0, "price": 1299, "isGem": True}]
    out = _js("r.scaleFor(" + __import__("json").dumps(rows) + ")")
    assert out["half"] == 0.25
    rows2 = [{"delta": 700, "price": 500, "isGem": False}, {"delta": 0, "price": 1299, "isGem": True}]
    out2 = _js("r.scaleFor(" + __import__("json").dumps(rows2) + ")")
    assert out2["half"] in (0.5, 0.75, 1.0)


def test_possible_rows_keep_their_delta_confidence_and_reason():
    result = {
        "gem": {"price": {"low": 1299.0}},
        "possible_matches": [{
            "shop": "amazon.in", "title": "M650 pack", "price": 2199.0,
            "confidence": 0.62, "why": "different pack",
        }],
    }
    row = _js("r.possibleRows(" + json.dumps(result) + ")[0]")
    assert row["delta"] == 900.0
    assert row["vsGem"] == "\u20b9900 more"
    assert row["confidence"] == 0.62
    assert row["confidenceText"] == "62%"
    assert row["confidenceWidth"] == 62
    assert row["why"] == "different pack"
    assert row["samePrice"] is False
    same = _js(
        "r.possibleRows({gem:{price:{low:1299}},possible_matches:[{shop:'a',price:1299}]})[0]"
    )
    assert same["delta"] == 0
    assert same["samePrice"] is True


def test_filter_finished_by_shop_gives_each_web_row_its_own_counts():
    stats = _js(
        "(() => { const s = r.newStats(); ["
        "{type:'shop_started',shop:'indiamart.com'},"
        "{type:'shop_started',shop:'croma.com'},"
        "{type:'filter_finished',shop:'other_shops',seen:5,sent:3,dropped:2,"
        "by_shop:{'indiamart.com':{seen:2,sent:2,dropped:0},'croma.com':{seen:3,sent:1,dropped:2}}}"
        "].forEach(e => r.applyEvent(s, e)); return s; })()"
    )
    rows = {row["key"]: row for row in _js("r.laneRows(" + json.dumps(stats) + ")")}
    assert rows["indiamart.com"]["kept"] == 2
    assert rows["indiamart.com"]["dropped"] == 0
    assert rows["croma.com"]["kept"] == 1
    assert rows["croma.com"]["dropped"] == 2


def test_lane_row_model_uses_plain_signs_and_a_single_confirmed_number():
    row = {
        "queries": 3, "queriesDone": 3, "seen": 12, "kept": 12, "dropped": 3,
        "judged": 5, "matches": 1, "possible": 0, "rejected": 4, "status": "found",
    }
    out = _js("r.laneRowModel(" + json.dumps(row) + ")")
    assert out["search"] == "3/3"
    assert out["filter"] == "+12 -3"
    assert out["verify"] == "+1 -4"
    assert out["confirmed"] == 1
    zero = _js("r.laneRowModel({seen:2,kept:0,dropped:0,matches:0,rejected:0})")
    assert zero["filter"] == "0"
    assert zero["confirmed"] == 0


def test_gem_product_event_keeps_image_and_specs_for_the_card():
    stats = _js(
        "(() => { const s = r.newStats(); r.applyEvent(s, {type:'gem_product',"
        "title:'M650',price:1299,brand:'Logitech',model:'M650',"
        "image:'https://mkp.gem.gov.in/a.jpg',specs:{Colour:'Graphite'}}); return s; })()"
    )
    assert stats["gem"]["image"] == "https://mkp.gem.gov.in/a.jpg"
    assert stats["gem"]["specs"] == {"Colour": "Graphite"}


def test_lane_rows_drops_marketplaces():
    stats = {"shops": {"flipkart.com": {}, "amazon.in": {}, "shop.in": {"seen": 3, "kept": 2, "dropped": 1}}}
    out = _js("r.laneRows(" + __import__("json").dumps(stats) + ")")
    assert [row["key"] for row in out] == ["shop.in"]
    assert out[0]["kept"] == 2


def test_signed_count_formats_signs():
    assert _js("r.signedCount(12)") == "+12"
    assert _js("r.signedCount(-3)") == "-3"
    assert _js("r.signedCount(0)") == "0"


def test_should_auto_collapse_only_at_done_without_manual_toggle():
    assert _js("r.shouldAutoCollapse(false, 'done')") is True
    assert _js("r.shouldAutoCollapse(true, 'done')") is False
    assert _js("r.shouldAutoCollapse(false, 'running')") is False
