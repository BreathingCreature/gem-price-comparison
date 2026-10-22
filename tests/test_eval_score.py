"""The answer-key scorer: reading the key, matching URLs, counting right and wrong."""
import csv
import json

import pytest

from eval import score

HEADER = score.KEY_COLUMNS


def _write_key(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in HEADER})


def _row(**kw):
    base = {"id": "P1", "gem_url": "https://mkp.gem.gov.in/x", "gem_name": "Mouse", "branded": "yes"}
    base.update(kw)
    return base


AMZ = "https://www.amazon.in/Some-Mouse-Title/dp/B0ABCDEFGH/ref=sr_1_1?crid=1"
AMZ_SAME = "https://www.amazon.in/dp/B0ABCDEFGH"
FK = "https://www.flipkart.com/some-mouse/p/itm123abc456?pid=ACCXYZ1234&lid=LST1"
FK_SAME = "https://www.flipkart.com/other-slug/p/itm999?pid=ACCXYZ1234&lid=LST1&q=mouse"


def test_url_key_ignores_title_slug_and_tracking_junk():
    assert score.url_key(AMZ) == score.url_key(AMZ_SAME) == "amazon.in/dp/b0abcdefgh"
    assert score.url_key(FK) == score.url_key(FK_SAME) == "flipkart.com?pid=ACCXYZ1234&lid=LST1"
    assert score.url_key("https://www.croma.com/x/p/123/?utm=1") == score.url_key("https://croma.com/x/p/123")


def test_url_key_keeps_real_variants_and_keeps_shops_apart():
    other_lid = "https://www.flipkart.com/some-mouse/p/itm123abc456?pid=ACCXYZ1234&lid=LST2"
    assert score.url_key(FK) != score.url_key(other_lid)
    croma_page_1 = "https://www.croma.com/x/p/123?page=1"
    croma_page_2 = "https://www.croma.com/x/p/123?page=2"
    assert score.url_key(croma_page_1) != score.url_key(croma_page_2)


def test_site_of():
    assert score.site_of("https://www.amazon.in/dp/B0ABCDEFGH") == "amazon"
    assert score.site_of("https://dl.flipkart.com/s/abc") == "flipkart"
    assert score.site_of("https://www.croma.com/x") == "croma.com"


def test_parse_cell_three_states_and_alternatives():
    assert score.parse_cell("") == ("unchecked", [])
    assert score.parse_cell("none") == ("none", [])
    assert score.parse_cell("https://a.in/dp/B0AAAAAAAA | https://a.in/dp/B0BBBBBBBB") == (
        "urls", ["https://a.in/dp/B0AAAAAAAA", "https://a.in/dp/B0BBBBBBBB"])


def test_check_key_accepts_a_good_key_and_flags_bad_rows():
    good = [_row(amazon_url=AMZ, amazon_price="500", flipkart_url="NONE")]
    problems, _ = score.check_key(good)
    assert problems == []

    bad = [
        _row(id="A", amazon_url=FK),                       # Flipkart link in the Amazon column
        _row(id="A", flipkart_url="NONE", branded="maybe"),  # repeated id and bad branded value
        _row(id="C", gem_url="not-a-url", amazon_url="NONE"),
        _row(id="D"),                                      # nothing checked
    ]
    text = "\n".join(score.check_key(bad)[0])
    assert "points at flipkart" in text
    assert "id used twice" in text
    assert "branded must be yes or no" in text
    assert "gem_url must start with http" in text
    assert "no shop was checked" in text


def test_perfect_run_scores_one():
    row = _row(amazon_url=AMZ, amazon_price="1000", flipkart_url=FK, flipkart_price="900")
    rec = {"matches": [
        {"url": AMZ_SAME, "price": 1005.0},
        {"url": FK_SAME, "price": 900.0},
    ], "seconds": 30}
    o = score.score_row(row, rec)
    assert (o["wanted"], o["found"], o["preds"], o["correct"]) == (2, 2, 2, 2)
    assert (o["price_checked"], o["price_ok"]) == (2, 2)


def test_price_outside_two_percent_is_not_accurate():
    row = _row(amazon_url=AMZ, amazon_price="1000")
    assert score.score_row(row, {"matches": [{"url": AMZ, "price": 1019.0}]})["price_ok"] == 1
    assert score.score_row(row, {"matches": [{"url": AMZ, "price": 1030.0}]})["price_ok"] == 0


def test_wrong_listing_is_a_false_positive_and_a_miss():
    row = _row(amazon_url=AMZ)
    o = score.score_row(row, {"matches": [{"url": "https://www.amazon.in/dp/B0ZZZZZZZZ", "price": 10}]})
    assert (o["wanted"], o["found"], o["preds"], o["correct"]) == (1, 0, 1, 0)


def test_none_means_any_prediction_there_is_wrong_and_nothing_is_missed():
    row = _row(amazon_url="NONE", flipkart_url=FK)
    o = score.score_row(row, {"matches": [{"url": AMZ, "price": 10}, {"url": FK, "price": 10}]})
    assert (o["wanted"], o["found"], o["preds"], o["correct"]) == (1, 1, 2, 1)


def test_shops_nobody_checked_are_not_judged():
    row = _row(amazon_url=AMZ)  # Flipkart and other shops left blank
    rec = {"matches": [{"url": AMZ, "price": 10}, {"url": FK, "price": 10}, {"url": "https://www.croma.com/p/1", "price": 10}]}
    o = score.score_row(row, rec)
    assert o["preds"] == 1 and o["correct"] == 1


def test_alternatives_any_one_is_enough():
    row = _row(amazon_url=AMZ + " https://www.amazon.in/dp/B0SECONDXX")
    o = score.score_row(row, {"matches": [{"url": "https://www.amazon.in/dp/B0SECONDXX", "price": 5}]})
    assert (o["found"], o["correct"]) == (1, 1)


def test_other_shop_urls_are_each_found_separately():
    row = _row(other_urls="https://www.croma.com/a/p/1 https://www.reliancedigital.in/b/p/2")
    rec = {"matches": [{"url": "https://www.croma.com/a/p/1/", "price": 5}]}
    o = score.score_row(row, rec)
    assert (o["wanted"], o["found"], o["preds"], o["correct"]) == (2, 1, 1, 1)


def test_summary_splits_branded_and_generic_and_reports_time(tmp_path):
    key = tmp_path / "key.csv"
    _write_key(key, [
        _row(id="B1", branded="yes", amazon_url=AMZ),
        _row(id="G1", branded="no", amazon_url="https://www.amazon.in/dp/B0GENERIC0"),
    ])
    results = {
        "B1": {"id": "B1", "matches": [{"url": AMZ, "price": 1}], "seconds": 10},
        "G1": {"id": "G1", "matches": [], "seconds": 30},
    }
    out = score.score(score.load_key(key), results)
    assert out["all"]["recall"] == 0.5 and out["all"]["precision"] == 1.0
    assert out["branded"]["recall"] == 1.0 and out["generic"]["recall"] == 0.0
    assert out["all"]["seconds_median"] == 20.0
    assert "| branded |" in score.render_markdown(out)


def test_products_missing_from_the_results_are_counted_as_not_run(tmp_path):
    key = tmp_path / "key.csv"
    _write_key(key, [_row(id="P1", amazon_url=AMZ)])
    out = score.score(score.load_key(key), {})
    assert out["all"]["not_run"] == 1 and out["all"]["recall"] == 0.0
    assert "no line in the results file" in score.render_markdown(out)


def test_command_line_end_to_end(tmp_path, capsys):
    key = tmp_path / "key.csv"
    res = tmp_path / "res.jsonl"
    _write_key(key, [_row(amazon_url=AMZ, amazon_price="100")])
    res.write_text(json.dumps({"id": "P1", "matches": [{"url": AMZ, "price": 100}], "seconds": 5}) + "\n")
    assert score.main(["--key", str(key), "--results", str(res), "--json", str(tmp_path / "o.json")]) == 0
    assert "| all | 1 |" in capsys.readouterr().out
    assert json.loads((tmp_path / "o.json").read_text())["all"]["recall"] == 1.0
    assert score.main(["--check-key", str(key)]) == 0


def test_command_line_refuses_a_broken_key(tmp_path, capsys):
    key = tmp_path / "key.csv"
    res = tmp_path / "res.jsonl"
    _write_key(key, [_row(id="")])
    res.write_text("")
    assert score.main(["--key", str(key), "--results", str(res)]) == 1
    assert "problems" in capsys.readouterr().out


def test_other_shops_per_product_recall_counts_one_right_listing(tmp_path):
    rows = [_row(id="P1", other_urls="https://a.in/p/1 https://b.in/p/2")]
    rec = {"matches": [{"url": "https://a.in/p/1", "price": 10}]}
    out = score.score_row(rows[0], rec)
    assert out["found"] == 1 and out["wanted"] == 2        # strict: 1 of 2 URLs
    assert out["other_wanted"] and out["other_any"]         # per product: yes
    s = score.summarize([out])
    assert s["other_product_recall"] == 1.0


def test_with_possible_adds_unconfirmed_listings(tmp_path, capsys):
    key = tmp_path / "k.csv"
    _write_key(key, [_row(amazon_url=AMZ_SAME, amazon_price="10")])
    res = tmp_path / "r.jsonl"
    res.write_text(json.dumps({"id": "P1", "matches": [], "possible": [{"url": AMZ_SAME, "price": 10}]}) + "\n")
    assert score.main(["--key", str(key), "--results", str(res)]) == 0
    assert "0.0%" in capsys.readouterr().out
    assert score.main(["--key", str(key), "--results", str(res), "--with-possible"]) == 0
    out = capsys.readouterr().out
    assert "possible" in out and "100.0%" in out
