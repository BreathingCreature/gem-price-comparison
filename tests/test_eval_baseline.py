"""The no-LLM baseline: title similarity only."""
import json

from eval import baseline_fuzzy as fz


def test_similarity_basics():
    assert fz.similarity("Logitech M235 Wireless Mouse", "logitech m235 wireless mouse") == 1.0
    assert fz.similarity("abc def", "xyz uvw") == 0.0
    assert fz.similarity("", "anything") == 0.0
    assert 0.0 < fz.similarity("Logitech M235 Mouse", "Logitech M330 Mouse") < 1.0


def _cand(url, domain, title, price):
    return {"url": url, "domain": domain, "title": title, "price": price}


def test_picks_the_best_listing_per_shop_above_the_threshold():
    cands = [
        _cand("https://a/1", "amazon.in", "Logitech M235 Wireless Mouse Grey", 1000.0),
        _cand("https://a/2", "amazon.in", "Logitech M235 Wireless Mouse", 1100.0),
        _cand("https://f/1", "flipkart.com", "Dell Keyboard", 500.0),
    ]
    out = fz.pick_matches("Logitech M235 Wireless Mouse", 1000.0, cands, threshold=0.5)
    assert [m["url"] for m in out] == ["https://a/2"]  # the closer one, and nothing from the keyboard


def test_price_band_option_drops_far_priced_listings():
    cands = [_cand("https://a/1", "amazon.in", "Logitech M235 Wireless Mouse", 39.0)]
    assert fz.pick_matches("Logitech M235 Wireless Mouse", 1000.0, cands)  # kept without the band
    assert fz.pick_matches("Logitech M235 Wireless Mouse", 1000.0, cands, price_band=True) == []


def test_candidates_come_out_of_a_trace_without_duplicates():
    trace = {"stages": {
        "flipkart_search": [{"candidates": [{"url": "https://f/1", "source_domain": "flipkart.com",
                                             "structured_fields": {"title": "Mouse", "price": "1,299"}}]}],
        "amazon_search": [{"candidates": [{"url": "https://f/1", "source_domain": "amazon.in",
                                           "structured_fields": {"title": "Mouse", "price": 5}},
                                          {"url": "https://a/9", "source_domain": "amazon.in", "structured_fields": None}]}],
        "generic_scrapes": [],
        "discovery": [],
    }}
    out = fz.candidates_from_trace(trace)
    assert len(out) == 1 and out[0]["price"] == 1299.0


def test_command_line_writes_a_scoreable_results_file(tmp_path):
    traces = tmp_path / "saved_traces"
    traces.mkdir()
    (traces / "t1.json").write_text(json.dumps({"stages": {
        "extraction": {"title": "Logitech M235 Wireless Mouse", "gem_price": 1000.0},
        "amazon_search": [{"candidates": [{"url": "https://www.amazon.in/dp/B0ABCDEFGH", "source_domain": "amazon.in",
                                           "structured_fields": {"title": "Logitech M235 Wireless Mouse", "price": 990}}]}],
    }}))
    res = tmp_path / "in.jsonl"
    res.write_text(json.dumps({"id": "P1", "trace_id": "t1", "seconds": 12}) + "\n"
                   + json.dumps({"id": "P2", "trace_id": "gone", "seconds": 3}) + "\n")
    out = tmp_path / "out.jsonl"
    assert fz.main(["--results", str(res), "--out", str(out), "--traces", str(traces)]) == 0
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert lines[0]["matches"][0]["url"].endswith("B0ABCDEFGH") and lines[0]["error"] is None
    assert lines[1]["matches"] == [] and "trace file not found" in lines[1]["error"]


def test_a_half_written_results_line_is_skipped(tmp_path):
    traces = tmp_path / "saved_traces"
    traces.mkdir()
    (traces / "t1.json").write_text(json.dumps({"stages": {
        "extraction": {"title": "Logitech M235 Wireless Mouse", "gem_price": 1000.0},
    }}))
    res = tmp_path / "in.jsonl"
    res.write_text(json.dumps({"id": "P1", "trace_id": "t1"}) + "\n" + '{"id": "P2", "tra')

    out = tmp_path / "out.jsonl"
    assert fz.main(["--results", str(res), "--out", str(out), "--traces", str(traces)]) == 0
    assert [json.loads(x)["id"] for x in out.read_text().splitlines()] == ["P1"]
