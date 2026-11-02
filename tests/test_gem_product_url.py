"""One table of GeM links, checked by the Python and the page-side rule."""
import json
from pathlib import Path

from core.gem_url import looks_like_gem_product_url

CASES = json.loads((Path(__file__).parent / "url_cases.json").read_text(encoding="utf-8"))


def test_the_server_accepts_only_product_links_from_the_shared_table():
    for case in CASES:
        assert looks_like_gem_product_url(case["url"]) is case["ok"], case["url"]


def test_a_search_page_is_still_a_gem_url_but_not_a_product_url():
    from core.gem_url import looks_like_gem_url

    search = "https://mkp.gem.gov.in/search?q=mouse"
    assert looks_like_gem_url(search) is True
    assert looks_like_gem_product_url(search) is False
