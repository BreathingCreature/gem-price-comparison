"""Save one real page as a test fixture.

    python tools/save_page.py flipkart "usb c cable"
    python tools/save_page.py amazon "https://www.amazon.in/s?k=usb+c+cable" --name amazon-search-usb
    python tools/save_page.py gem "https://mkp.gem.gov.in/..."

The page is written to tests/fixtures/pages/<name>.html so a parser test can
run offline against the exact page that broke. Run this by hand when a live
page changes; nothing in the test suite calls it and it is never run
automatically (it does hit the network).

A file is only written when something came back; a blocked page is saved too
(that is often the interesting one) and the tool says so.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SHOPS = ("flipkart", "amazon", "gem", "generic")
FIXTURES_DIR = ROOT / "tests" / "fixtures" / "pages"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "page"


def default_name(shop: str, target: str) -> str:
    return f"{shop}-{_slug(target)}"


def fetch_html(shop: str, target: str, *, fetcher=None) -> str | None:
    """The raw page for a query (flipkart/amazon) or any URL."""
    if fetcher is not None:
        return fetcher(shop, target)
    if "://" in target:
        from curl_cffi import requests as curl_requests

        resp = curl_requests.get(target, impersonate="chrome150", timeout=30,
                                 headers={"User-Agent": _UA, "Accept-Language": "en-IN,en;q=0.9"})
        return resp.text
    if shop == "flipkart":
        from scrapers import flipkart

        return flipkart._fetch_light_page(target)
    if shop == "amazon":
        from scrapers import amazon

        return amazon._fetch_curl_cffi_page(target)
    raise ValueError(f"{shop} needs a full URL, not a search query")


def save_page(shop: str, target: str, *, name: str | None = None, fetcher=None,
              folder: Path | None = None) -> Path:
    html = fetch_html(shop, target, fetcher=fetcher)
    if not html:
        raise RuntimeError(f"{shop} returned nothing for {target!r}; nothing saved")
    path = Path(folder or FIXTURES_DIR) / f"{name or default_name(shop, target)}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Save one real page as a test fixture.")
    parser.add_argument("shop", choices=SHOPS, help="which fetcher to use")
    parser.add_argument("target", help="a search query (flipkart/amazon) or a full URL")
    parser.add_argument("--name", help="file name without .html (default: shop + the target)")
    args = parser.parse_args(argv)
    try:
        path = save_page(args.shop, args.target, name=args.name)
    except Exception as exc:
        print(f"could not save the page: {type(exc).__name__}: {exc}")
        return 1
    size = path.stat().st_size
    print(f"saved {path} ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
