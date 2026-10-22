"""Pooling: extra listings that a person says are right are added to a copy of the key."""
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
