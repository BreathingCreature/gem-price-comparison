"""Fetch one icon per shop domain and save 64 px PNGs for the page.

Run once on a connected machine, then commit frontend/shops/ and its manifest:

    .venv\\Scripts\\python.exe tools/fetch_shop_icons.py

For each domain the tool tries /favicon.ico first, then reads the home page and
uses the largest declared rel icon (including apple-touch-icon). The result is
converted to a 64 px RGBA PNG with Pillow, which is a development-only
dependency. Domains that fail are listed in the manifest instead of guessed.

The page still falls back to GET /api/icon/{domain} and then a neutral globe,
so a missing file is never drawn as a letter tile.
"""
from __future__ import annotations

import io
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parent.parent
SHOPS_FILE = ROOT / "discovery" / "indian_shops.txt"
OUT_DIR = ROOT / "frontend" / "shops"
CACHE_DIR = ROOT / "cache"
EVAL_RESULTS = ROOT / "eval" / "results"
EXTRA_DOMAINS = ("flipkart.com", "amazon.in", "mkp.gem.gov.in")
TOP_DOMAINS = 40
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = 6
MAX_BYTES = 2_000_000
ICON_MIME = ("image/", "image/x-icon", "image/vnd.microsoft.icon")
LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
ATTR_RE = re.compile(r"([a-zA-Z-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")


def _domains() -> list[str]:
    found: list[str] = []
    if SHOPS_FILE.exists():
        for line in SHOPS_FILE.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                found.append(value.lower())
    for domain in EXTRA_DOMAINS:
        if domain not in found:
            found.append(domain)
    return found


_URL_HOST_RE = re.compile(r"https?://([a-z0-9][a-z0-9.-]*\.[a-z]{2,})", re.I)
_SHOP_KEY_RE = re.compile(r'"(?:shop|domain)"\s*:\s*"([a-z0-9][a-z0-9.-]*\.[a-z]{2,})"', re.I)
# libcurl links its error page in fetch failures, so it is not a shop.
_IGNORED_HOSTS = {"curl.se"}


def _record_domains(limit: int = TOP_DOMAINS) -> list[str]:
    """Most frequent hosts in saved runs, traces and eval results."""
    counts: Counter[str] = Counter()
    paths = []
    for folder, pattern in ((CACHE_DIR / "runs", "*.json"), (CACHE_DIR / "traces", "*.json"), (EVAL_RESULTS, "*.jsonl")):
        if folder.exists():
            paths.extend(sorted(folder.glob(pattern)))
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for host in _URL_HOST_RE.findall(text):
            host = host.lower().removeprefix("www.")
            if host not in _IGNORED_HOSTS:
                counts[host] += 1
        for host in _SHOP_KEY_RE.findall(text):
            host = host.lower().removeprefix("www.")
            if host not in _IGNORED_HOSTS:
                counts[host] += 1
    return [host for host, _ in counts.most_common(limit)]


def _stdin_domains() -> list[str]:
    if sys.stdin is None or sys.stdin.isatty():
        return []
    text = sys.stdin.read()
    return [line.strip().lower() for line in text.splitlines() if line.strip()]


def _wanted_domains() -> list[str]:
    found = _domains() + _record_domains() + _stdin_domains()
    seen = set()
    unique = []
    for domain in found:
        if domain in seen:
            continue
        seen.add(domain)
        unique.append(domain)
    return unique


def _links(html: str, base: str) -> list[tuple[int, str]]:
    """Declared icons, largest first. A missing size sorts last."""
    items: list[tuple[int, str]] = []
    for tag in LINK_RE.findall(html):
        attrs = {name.lower(): value.strip("\"'") for name, value in ATTR_RE.findall(tag)}
        rel = attrs.get("rel", "").lower()
        if "icon" not in rel or not attrs.get("href"):
            continue
        weight = 0
        sizes = attrs.get("sizes", "").lower()
        for width in re.findall(r"(\d+)x\d+", sizes):
            weight = max(weight, int(width))
        if "apple-touch-icon" in rel:
            weight = max(weight, 180)
        items.append((weight, urljoin(base, attrs["href"])))
    items.sort(key=lambda item: item[0], reverse=True)
    return items


def _get(url: str, allowed: tuple[str, ...], max_bytes: int = MAX_BYTES):
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=True,
        )
    except requests.RequestException:
        return None
    try:
        if response.status_code != 200:
            return None
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not content_type.startswith(allowed):
            return None
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(8192):
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        response.close()


def _save_png(data: bytes, path: Path) -> bool:
    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit("Pillow is required: .venv\\Scripts\\python.exe -m pip install pillow") from exc
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        return False
    image = image.convert("RGBA")
    image.thumbnail((64, 64), Image.LANCZOS)
    canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    canvas.paste(image, ((64 - image.width) // 2, (64 - image.height) // 2), image)
    canvas.save(path, "PNG", optimize=True)
    return True


def _icon_for(domain: str) -> bytes | None:
    data = _get(f"https://{domain}/favicon.ico", ICON_MIME)
    if data:
        return data
    html_bytes = _get(f"https://{domain}/", ("text/html", "application/xhtml+xml"), max_bytes=8_000_000)
    if not html_bytes:
        return None
    html = html_bytes.decode("utf-8", "replace")
    for _, url in _links(html, f"https://{domain}/"):
        if not url.lower().startswith("https://"):
            continue
        data = _get(url, ICON_MIME)
        if data:
            return data
    return None


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"icons": {}, "failures": []}
    manifest_path = OUT_DIR / "manifest.json"
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if isinstance(existing.get("icons"), dict):
            manifest["icons"] = dict(existing["icons"])
        if isinstance(existing.get("failures"), list):
            manifest["failures"] = list(existing["failures"])
    for domain in _wanted_domains():
        path = OUT_DIR / f"{domain}.png"
        data = _icon_for(domain)
        if data and _save_png(data, path):
            manifest["icons"][domain] = path.name
            if domain in manifest["failures"]:
                manifest["failures"].remove(domain)
            print(f"ok   {domain}")
        else:
            if domain not in manifest["failures"]:
                manifest["failures"].append(domain)
            print(f"fail {domain}")
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"\n{len(manifest['icons'])} saved, {len(manifest['failures'])} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
