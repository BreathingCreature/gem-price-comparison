"""Health check for the whole project. Run it after any change.

    python tools/doctor.py                 offline checks, takes a second
    python tools/doctor.py --live-llm      also sends one tiny message to every enabled LLM endpoint
    python tools/doctor.py --live-scrape   also runs one real Flipkart and Amazon search
    python tools/doctor.py --live-failover also breaks the first LLM endpoint on purpose and checks another one answers

Each line starts with PASS, WARN or FAIL. The exit code is 1 if anything
FAILED, so a script (or an agent) can rely on it. WARN means "works, but
look at this".
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

REQUIRED_PACKAGES = ["requests", "bs4", "lxml", "selenium", "dotenv", "openai", "curl_cffi", "fastapi", "uvicorn"]
FRONTEND_FILES = ["index.html", "render.js", "app.js", "style.css"]


def check_python():
    ok = sys.version_info >= (3, 10)
    return (PASS if ok else FAIL), f"Python {sys.version.split()[0]}" + ("" if ok else " (need 3.10 or newer)")


def check_packages():
    missing = []
    for name in REQUIRED_PACKAGES:
        try:
            importlib.import_module(name)
        except Exception:
            missing.append(name)
    if missing:
        return FAIL, "missing packages: " + ", ".join(missing) + "  (run: pip install -r requirements.txt)"
    return PASS, "all required packages import"


def check_modules():
    failures = []
    for name in ("cli", "core.pipeline", "llm.client", "llm.router", "llm.match", "scrapers.flipkart",
                 "scrapers.amazon", "scrapers.generic", "scrapers.gem", "discovery.discover", "api.server"):
        try:
            importlib.import_module(name)
        except Exception as exc:
            failures.append(f"{name} ({type(exc).__name__}: {exc})")
    if failures:
        return FAIL, "cannot import: " + "; ".join(failures)
    return PASS, "every project module imports"


def check_llm_config():
    from llm.router import load_endpoints

    try:
        endpoints, strategy = load_endpoints()
    except Exception as exc:
        return FAIL, f"LLM provider config is broken: {type(exc).__name__}: {exc}"
    usable = [e for e in endpoints if e.enabled]
    summary = ", ".join(f"{e.name}={'on' if e.enabled else 'off (' + e.disabled_reason + ')'}" for e in endpoints)
    if not usable:
        return WARN, f"no usable LLM endpoint, runs will fail: {summary}"
    if len(usable) == 1:
        return WARN, f"only one usable endpoint, so no fallback if it is overloaded ({strategy}): {summary}"
    return PASS, f"{len(usable)} usable endpoints ({strategy}): {summary}"


def check_folders():
    from core import pipeline

    for folder in (pipeline.CACHE_DIR, pipeline.TRACES_DIR):
        probe = Path(folder) / ".doctor-probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return FAIL, f"cannot write to {folder}: {exc}"
    return PASS, "cache and traces folders are writable"


def check_frontend_files():
    missing = [f for f in FRONTEND_FILES if not (ROOT / "frontend" / f).exists()]
    if missing:
        return FAIL, "frontend/ is missing: " + ", ".join(missing)
    return PASS, "frontend files are present"


def check_demo_run():
    """Runs the pretend pipeline and checks the event story is complete."""
    from api import demo

    previous = demo.DEMO_DELAY
    demo.DEMO_DELAY = 0.0
    seen = []
    try:
        result = demo.demo_runner("https://mkp.gem.gov.in/demo", False, seen.append)
    except Exception as exc:
        return FAIL, f"demo run crashed: {type(exc).__name__}: {exc}"
    finally:
        demo.DEMO_DELAY = previous
    types = [e["type"] for e in seen]
    needed = ["run_started", "gem_product", "search_finished", "candidate_verdict", "run_finished", "done"]
    missing = [t for t in needed if t not in types]
    if missing or not result.get("matches"):
        return FAIL, f"demo run is missing events {missing} or matches"
    return PASS, f"demo run produced {len(seen)} events and {len(result['matches'])} matches"


def check_live_llm():
    from llm.router import Router, load_endpoints

    try:
        endpoints, _ = load_endpoints()
    except Exception as exc:
        return [(FAIL, f"config broken: {exc}")]
    results = []
    for endpoint in [e for e in endpoints if e.enabled]:
        single = Router([dataclasses.replace(endpoint)], max_wait=1.0)
        if not single.endpoints[0].enabled:
            # A 404 remembered from an earlier run; not a new problem.
            results.append((WARN, f"{endpoint.name} skipped: {single.endpoints[0].disabled_reason}"))
            continue
        started = time.monotonic()
        try:
            # 256, not 50: a reasoning model spends most of a small budget
            # thinking and then answers with empty content.
            reply = single.complete([{"role": "user", "content": 'Reply with exactly: {"ok": true}'}], 256, purpose="doctor")
            results.append((PASS, f"{endpoint.name} answered in {time.monotonic() - started:.1f}s: {reply[:60]!r}"))
        except Exception as exc:
            results.append((FAIL, f"{endpoint.name} failed: {exc}"))
    if not results:
        results.append((WARN, "no enabled endpoint to test"))
    return results


SHOP_CANARIES = (("flipkart", "usb c cable"), ("amazon", "usb c cable"))
ENGINE_CANARY_QUERY = "logitech m650 price india"
DEBUG_HTML_DIR = ROOT / "debug_html"


def check_live_engines():
    """Asks every active discovery engine one query. No engine, no warning storm."""
    from discovery import discover

    try:
        engines = discover._active_engines()
    except Exception as exc:
        return [(FAIL, f"cannot list discovery engines: {type(exc).__name__}: {exc}")]
    if not engines:
        return [(WARN, "no discovery engine is active right now")]
    results = []
    for name, fn in engines.items():
        started = time.monotonic()
        try:
            urls, blocked = fn(ENGINE_CANARY_QUERY, None)
        except Exception as exc:
            results.append((FAIL, f"engine {name} crashed: {type(exc).__name__}: {exc}"))
            continue
        took = time.monotonic() - started
        if blocked:
            results.append((WARN, f"engine {name} showed a CAPTCHA (it rests for {discover.ENGINE_SKIP_SECONDS:.0f}s)"))
        elif not urls:
            results.append((WARN, f"engine {name} returned no links in {took:.1f}s"))
        else:
            results.append((PASS, f"engine {name} returned {len(urls)} link(s) in {took:.1f}s"))
    return results


def _canary_page_html(shop, query):
    """The raw page a canary saw, best effort --- only used to save evidence."""
    from scrapers import amazon, flipkart

    try:
        if shop == "flipkart":
            return flipkart._fetch_light_page(query)
        return amazon._fetch_curl_cffi_page(query)
    except Exception:
        return None


def _save_canary_page(shop, query, html):
    DEBUG_HTML_DIR.mkdir(parents=True, exist_ok=True)
    slug = "".join(ch if ch.isalnum() else "-" for ch in query.lower()).strip("-")[:40]
    path = DEBUG_HTML_DIR / f"{shop}-{slug}-{int(time.time())}.html"
    path.write_text(html or "", encoding="utf-8")
    return path


def check_live_shops():
    """One known query per shop; the parser must produce at least one card."""
    from scrapers import amazon, flipkart

    modules = {"flipkart": flipkart, "amazon": amazon}
    results = []
    for shop, query in SHOP_CANARIES:
        started = time.monotonic()
        try:
            found = modules[shop].search(query)
        except Exception as exc:
            found, error = None, f"{type(exc).__name__}: {exc}"
        else:
            error = found.error
        took = time.monotonic() - started
        if found is not None and found.candidates and not found.blocked and not error:
            results.append((PASS, f"shop {shop} parsed {len(found.candidates)} card(s) in {took:.0f}s"))
            continue
        path = _save_canary_page(shop, query, _canary_page_html(shop, query))
        if found is not None and found.blocked:
            results.append((WARN, f"shop {shop} blocked the canary (page saved: {path})"))
        elif error:
            results.append((FAIL, f"shop {shop} canary error: {error} (page saved: {path})"))
        else:
            results.append((WARN, f"shop {shop} parsed 0 cards in {took:.0f}s (page saved: {path})"))
    return results


def check_live_scrape():
    from scrapers import amazon, flipkart

    results = []
    for name, module in (("flipkart", flipkart), ("amazon", amazon)):
        started = time.monotonic()
        try:
            found = module.search("usb c cable")
        except Exception as exc:
            results.append((FAIL, f"{name} search crashed: {type(exc).__name__}: {exc}"))
            continue
        took = time.monotonic() - started
        if found.blocked:
            results.append((WARN, f"{name} blocked the search (CAPTCHA) after {took:.0f}s"))
        elif found.error:
            results.append((FAIL, f"{name} search error: {found.error}"))
        elif not found.candidates:
            results.append((WARN, f"{name} returned 0 products in {took:.0f}s (page layout may have changed)"))
        else:
            results.append((PASS, f"{name} returned {len(found.candidates)} products in {took:.0f}s"))
    return results


BAD_KEY = "invalid-key-for-failover-test"


def check_live_failover():
    """Gives the first usable provider a wrong key, then asks for an answer.
    A healthy setup shows the first endpoint rejected and a later one serving."""
    from llm.router import Router, load_endpoints

    endpoints, _ = load_endpoints()
    usable = [e for e in endpoints if e.enabled]
    if len(usable) < 2:
        return [(WARN, "failover test needs two usable endpoints, skipped")]
    first = usable[0]
    original = os.environ.get(first.api_key_env)
    os.environ[first.api_key_env] = BAD_KEY  # the router reads the key when it builds the client
    try:
        router = Router([dataclasses.replace(e) for e in usable], strategy="failover", max_wait=5.0)
        try:
            reply = router.complete([{"role": "user", "content": 'Reply with exactly: {"ok": true}'}], 50, purpose="doctor")
        except Exception as exc:
            return [(FAIL, f"no endpoint answered after breaking {first.name}: {exc}")]
        rows = {r["endpoint"]: r for r in router.status()}
    finally:
        if original is None:
            os.environ.pop(first.api_key_env, None)
        else:
            os.environ[first.api_key_env] = original
    if rows[first.name]["state"] == "ready":
        return [(WARN, f"{first.name} did not reject the wrong key, so failover was not demonstrated")]
    served_by = [name for name, r in rows.items() if r["calls"] > 0]
    return [(PASS, f"{first.name} was rejected ({rows[first.name]['state']}) and {', '.join(served_by)} answered: {reply[:40]!r}")]


OFFLINE_CHECKS = [check_python, check_packages, check_modules, check_llm_config, check_folders, check_frontend_files, check_demo_run]


def run_checks(live_llm: bool = False, live_scrape: bool = False, live_failover: bool = False,
               live_engines: bool = False, live_shops: bool = False) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    for check in OFFLINE_CHECKS:
        try:
            results.append(check())
        except Exception as exc:  # a check must never crash the doctor
            results.append((FAIL, f"{check.__name__} crashed: {type(exc).__name__}: {exc}"))
    if live_llm:
        results.extend(check_live_llm())
    if live_engines:
        results.extend(check_live_engines())
    if live_scrape:
        results.extend(check_live_scrape())
    if live_shops:
        results.extend(check_live_shops())
    if live_failover:
        results.extend(check_live_failover())
    return results


def _age_seconds(path: Path) -> float:
    return time.time() - path.stat().st_mtime


def clean_old_files(older_than_days: float) -> list[tuple[str, str]]:
    """Removes traces, run records and saved debug pages older than the limit."""
    from core import pipeline, runs

    cutoff = max(0.0, float(older_than_days)) * 86400.0
    results = []
    for label, folder, pattern in (
        ("trace", Path(pipeline.TRACES_DIR), "*.json"),
        ("run record", Path(runs.runs_dir()), "*.json"),
        ("debug page", DEBUG_HTML_DIR, "*.html"),
    ):
        if not folder.exists():
            results.append((PASS, f"no {label}s to clean ({folder} does not exist)"))
            continue
        removed = 0
        failed = 0
        for path in folder.glob(pattern):
            try:
                if _age_seconds(path) < cutoff:
                    continue
                path.unlink()
                removed += 1
            except OSError:
                failed += 1
        if failed:
            results.append((FAIL, f"could not remove {failed} {label}(s) from {folder}"))
        else:
            results.append((PASS, f"removed {removed} old {label}(s) from {folder} (older than {older_than_days:g}d)"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Health check for the GeM price comparison project.")
    parser.add_argument("--live", action="store_true",
                        help="run every live check: LLM endpoints, search engines and shop canaries")
    parser.add_argument("--live-llm", action="store_true", help="send one tiny message to each enabled LLM endpoint")
    parser.add_argument("--live-engines", action="store_true", help="ask each active search engine one query")
    parser.add_argument("--live-shops", action="store_true", help="run one canary query per shop and parse the page")
    parser.add_argument("--live-scrape", action="store_true", help="run one real Flipkart and Amazon search")
    parser.add_argument("--live-failover", action="store_true", help="break the first LLM endpoint on purpose and check another answers")
    parser.add_argument("--clean", action="store_true", help="remove old traces, run records and saved debug pages")
    parser.add_argument("--older-than", type=float, default=None,
                        help="age in days for --clean (default: TRACE_MAX_AGE_DAYS)")
    args = parser.parse_args()

    if args.clean:
        from core import config

        days = args.older_than if args.older_than is not None else config.TRACE_MAX_AGE_DAYS
        results = clean_old_files(days)
        for status, message in results:
            print(f"{status}  {message}")
        return 1 if any(status == FAIL for status, _ in results) else 0

    results = run_checks(
        live_llm=args.live or args.live_llm,
        live_scrape=args.live or args.live_scrape,
        live_failover=args.live or args.live_failover,
        live_engines=args.live or args.live_engines,
        live_shops=args.live or args.live_shops,
    )
    for status, message in results:
        print(f"{status}  {message}")
    failed = sum(1 for status, _ in results if status == FAIL)
    warned = sum(1 for status, _ in results if status == WARN)
    print(f"\n{len(results) - failed - warned} passed, {warned} warning(s), {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
