"""One listing, one key: the shared URL identity used by dedup and scoring."""
import pytest

from core.keys import listing_key

SAME = [
    (
        "https://www.amazon.in/Some-Mouse-Title/dp/B0ABCDEFGH/ref=sr_1_1?crid=1",
        "https://amazon.in/dp/B0ABCDEFGH?tag=affiliate-21",
    ),
    ("https://www.amazon.in/dp/B0ABCDEFGH", "https://www.amazon.in/gp/product/B0ABCDEFGH"),
    ("https://www.amazon.in/Title/dp/B0ABCDEFGH?th=1&psc=1", "https://amazon.in/dp/B0ABCDEFGH"),
    ("https://www.amazon.in/dp/b0abcdefgh", "https://www.amazon.in/Other/dp/B0ABCDEFGH"),
    ("https://www.flipkart.com/some-mouse/p/itm123?pid=ACC123&lid=LST1", "https://flipkart.com/other/p/itm999?lid=LST1&pid=ACC123&utm_source=x"),
    ("https://www.flipkart.com/some-mouse/p/itm123?pid=ACC123", "https://flipkart.com/x/p/itm999?pid=ACC123&q=mouse"),
    ("https://www.flipkart.com/x/p/itm123abc", "https://flipkart.com/y/p/ITM123ABC?utm_medium=email"),
    ("https://www.croma.com/x/p/123/?utm=1", "https://croma.com/x/p/123"),
    ("https://www.croma.com/x/p/123?utm_source=a&utm_campaign=b", "https://croma.com/x/p/123?utm_medium=c"),
    ("https://www.croma.com/x/p/123#reviews", "https://croma.com/x/p/123/"),
    ("https://shop.example.com/x?b=2&a=1", "https://shop.example.com/x?a=1&b=2"),
    ("https://shop.example.com/x?th=1", "https://shop.example.com/x?th=1&pf_rd_p=abc&pd_rd_r=def"),
    ("https://shop.example.com/x?ref=home&tag=deal", "https://shop.example.com/x"),
    ("https://SHOP.example.com/X", "https://shop.example.com/X"),
]

DIFFERENT = [
    ("https://www.amazon.in/dp/B0ABCDEFGH", "https://www.amazon.in/dp/B0ABCDEFGI"),
    ("https://www.amazon.in/dp/B0ABCDEFGH", "https://www.amazon.com/dp/B0ABCDEFGH"),
    ("https://www.flipkart.com/p/itm1?pid=ACC123&lid=LST1", "https://www.flipkart.com/p/itm1?pid=ACC123&lid=LST2"),
    ("https://www.flipkart.com/p/itm1?pid=ACC123", "https://www.flipkart.com/p/itm1?pid=ACC124"),
    ("https://www.croma.com/x/p/123?page=1", "https://www.croma.com/x/p/123?page=2"),
    ("https://shop.example.com/x", "https://other.example.com/x"),
    ("https://shop.example.com/x?color=red", "https://shop.example.com/x?color=blue"),
]


@pytest.mark.parametrize("left,right", SAME)
def test_urls_that_are_the_same_listing(left, right):
    assert listing_key(left) == listing_key(right)


@pytest.mark.parametrize("left,right", DIFFERENT)
def test_urls_that_are_different_listings(left, right):
    assert listing_key(left) != listing_key(right)


def test_exact_shapes():
    assert listing_key("https://www.amazon.in/Title/dp/B0ABCDEFGH/ref=x") == "amazon.in/dp/b0abcdefgh"
    assert listing_key("https://www.flipkart.com/slug/p/itm1?pid=ACC123&lid=LST1") == "flipkart.com?pid=ACC123&lid=LST1"
    assert listing_key("https://www.croma.com/x/p/123/") == "croma.com/x/p/123"


def test_no_url_gives_an_empty_key():
    assert listing_key("") == ""
    assert listing_key("   ") == ""
    assert listing_key(None) == ""
