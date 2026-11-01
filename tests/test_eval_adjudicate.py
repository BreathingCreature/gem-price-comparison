"""Pooling: extra listings that a person says are right are added to a copy of the key."""
import csv
import json

from eval import adjudicate, score

KEY = [{"id": "P1", "gem_url": "https://mkp.gem.gov.in/x", "gem_name": "Paper", "branded": "yes",
        "amazon_url": "NONE", "flipkart_url": "https://www.flipkart.com/a/p/itm1?pid=AAA&lid=L1",
        "other_urls": "https://shop.in/p/1"}]
RES = {"P1": {"matches": [
    {"url": "https://www.flipkart.com/a/p/itm1?pid=AAA&lid=L1", "price": 1},   # already in the key
    {"url": "https://www.flipkart.com/b/p/itm2?pid=BBB&lid=L2", "price": 2},   # extra
    {"url": "https://www.amazon.in/dp/B0ABCDEFGH", "price": 3},                 # extra, key says NONE
    {"url": "https://other.in/p/9", "price": 4},                                # extra
]}}


def test_review_lists_only_matches_the_key_does_not_know():
    items = adjudicate.extra_matches(KEY, RES)
    assert [i["site"] for i in items] == ["flipkart", "amazon", "other.in"]


def test_pool_adds_only_yes_and_flags_a_changed_none():
    items = adjudicate.extra_matches(KEY, RES)
    items[0]["verdict"] = "yes"
    items[1]["verdict"] = "yes"
    items[2]["verdict"] = "no"
    rows, report = adjudicate.pool(KEY, items)
    assert "itm2" in rows[0]["flipkart_url"] and "itm1" in rows[0]["flipkart_url"]
    assert "B0ABCDEFGH" in rows[0]["amazon_url"]
    assert "other.in" not in rows[0]["other_urls"]
    assert any("NONE" in line for line in report)
    assert KEY[0]["amazon_url"] == "NONE"          # the original rows are not touched


def test_pooled_key_scores_higher_only_because_a_person_said_yes():
    items = adjudicate.extra_matches(KEY, RES)
    for i in items:
        i["verdict"] = "yes"
    rows, _ = adjudicate.pool(KEY, items)
    strict = score.score_row(KEY[0], RES["P1"])
    pooled = score.score_row(rows[0], RES["P1"])
    assert pooled["correct"] > strict["correct"]


def test_pool_reports_an_unknown_id_and_does_not_add_a_known_url():
    items = [
        {"id": "P9", "url": "https://shop.in/p/9", "verdict": "yes"},
        {"id": "P1", "url": KEY[0]["flipkart_url"], "verdict": "yes"},
    ]

    rows, report = adjudicate.pool(KEY, items)

    assert report[0] == "P9: not in the key, skipped"
    assert rows[0]["flipkart_url"] == KEY[0]["flipkart_url"]
    assert report[-1] == "0 listing(s) added to the key"


def test_write_review_uses_the_verdict_columns(tmp_path):
    path = tmp_path / "review.csv"
    items = adjudicate.extra_matches(KEY, RES)

    adjudicate.write_review(path, items)

    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == adjudicate.REVIEW_COLUMNS
    assert rows[0]["site"] == "flipkart" and rows[0]["verdict"] == ""


def test_write_key_keeps_every_key_column_and_selected_values(tmp_path):
    path = tmp_path / "pooled.csv"
    rows, _ = adjudicate.pool(KEY, [])

    adjudicate.write_key(path, rows)

    with path.open(newline="", encoding="utf-8") as f:
        written = list(csv.DictReader(f))
    assert list(written[0]) == score.KEY_COLUMNS
    assert written[0]["id"] == "P1" and written[0]["gem_name"] == "Paper"


def _write_key(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=score.KEY_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def test_main_review_writes_the_extra_matches(tmp_path, capsys):
    key_path = tmp_path / "key.csv"
    _write_key(key_path, KEY)
    results_path = tmp_path / "results.jsonl"
    results_path.write_text(json.dumps({"id": "P1", "matches": RES["P1"]["matches"]}) + "\n", encoding="utf-8")
    out = tmp_path / "review.csv"

    assert adjudicate.main(["review", "--key", str(key_path), "--results", str(results_path), "--out", str(out)]) == 0

    assert "3 extra match(es) written" in capsys.readouterr().out
    with out.open(newline="", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 3


def test_main_pool_writes_a_new_key_and_refuses_to_replace_the_original(tmp_path, capsys):
    key_path = tmp_path / "key.csv"
    _write_key(key_path, KEY)
    review = tmp_path / "review.csv"
    review.write_text(
        "id,gem_name,gem_url,site,url,price,confidence,verdict,note\n"
        "P1,Paper,https://mkp.gem.gov.in/x,flipkart,https://www.flipkart.com/new/p/itm9,2,80,yes,\n",
        encoding="utf-8",
    )
    out = tmp_path / "pooled.csv"

    assert adjudicate.main(["pool", "--key", str(key_path), "--review", str(review), "--out", str(out)]) == 0
    assert "1 listing(s) added to the key" in capsys.readouterr().out
    assert "itm9" in out.read_text(encoding="utf-8")
    assert "itm9" not in key_path.read_text(encoding="utf-8")

    assert adjudicate.main(["pool", "--key", str(key_path), "--review", str(review), "--out", str(key_path)]) == 2
    assert "Refusing to overwrite the original key" in capsys.readouterr().out
