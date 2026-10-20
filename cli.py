"""Command line: one GeM link in, one comparison out.

    python cli.py <gem_url> [--refresh] [--no-wait] [--json] [--strict] [--fast]

The pipeline prints its first honest result as soon as the first pass is
done, then keeps the terminal updated for anything the retry changes. All
the actual logic lives in core/pipeline.py - this file is presentation,
argument parsing and the little bit of wait-limit bookkeeping.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

DEFAULT_RETRY_WAIT_SECONDS = 120.0
WAITING_LINE = "Waiting for retries (up to {limit:.0f} s, Ctrl+C to stop)\u2026"


def _clock() -> float:
    """Monotonic seconds. Tests replace this to fake the wait limit."""
    return time.monotonic()


def _wait_limit() -> float:
    raw = os.getenv("GEM_RETRY_CLI_WAIT", "")
    if not raw.strip():
        return DEFAULT_RETRY_WAIT_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_RETRY_WAIT_SECONDS


def format_price(value) -> str:
    if value is None:
        return "\u2014"
    return f"\u20b9{value:,.2f}"


def _gem_view(result: dict) -> dict:
    """The v2 `gem` block if present, else the old `gem_product` fields."""
    gem = result.get("gem")
    if isinstance(gem, dict) and gem:
        price = gem.get("price") if isinstance(gem.get("price"), dict) else {}
        return {
            "title": gem.get("title"),
            "price": price.get("low"),
            "kind": price.get("kind") or "unknown",
        }
    product = result.get("gem_product") or {}
    return {
        "title": product.get("title"),
        "price": product.get("gem_price"),
        "kind": product.get("gem_price_type") or "unknown",
    }


def _cheapest_view(result: dict):
    cheapest = result.get("cheapest")
    if isinstance(cheapest, dict) and cheapest.get("price") is not None:
        return cheapest
    comparison = result.get("comparison") or {}
    price = comparison.get("cheapest_match_price")
    if price is None:
        return None
    # Old (v1) results have no `cheapest` block: find the row at that price so
    # the line can still name the shop.
    row = next((m for m in _match_rows(result) if m.get("price") == price), None)
    return {
        "shop": row["shop"] if row else None,
        "price": price,
        "saving_vs_gem": comparison.get("savings_vs_gem"),
        "url": row["url"] if row else "",
    }


def _match_rows(result: dict) -> list[dict]:
    """Old and new result shapes both reduce to shop/price/confidence/url."""
    rows = []
    for match in result.get("matches") or []:
        rows.append(
            {
                "shop": match.get("shop") or match.get("source_domain") or "?",
                "price": match.get("price") if "price" in match else match.get("price_used"),
                "confidence": match.get("confidence"),
                "url": match.get("url") or match.get("candidate_url") or "",
                "reason": match.get("reason") or "",
                "in_stock": match.get("in_stock", True),
            }
        )
    return rows


def _cheapest_text(result: dict) -> str:
    best = _cheapest_view(result)
    if best is None:
        return "not found yet"
    line = format_price(best.get("price"))
    if best.get("shop"):
        line += f" on {best['shop']}"
    saving = best.get("saving_vs_gem")
    if saving is not None:
        word = "less" if saving >= 0 else "more"
        line += f"  ({format_price(abs(saving))} {word} than GeM)"
    return line


def print_result(gem_url: str, result: dict) -> None:
    gem = _gem_view(result)
    rows = _match_rows(result)

    print()
    print("GeM Price Comparison")
    print("=" * 60)
    print(f"Product  : {gem.get('title') or '(title not extracted)'}")
    print(f"GeM price: {format_price(gem.get('price'))} ({gem.get('kind')})")
    print(f"URL      : {gem_url}")
    print(f"Cheapest : {_cheapest_text(result)}")
    print()

    for warning in result.get("warnings") or []:
        print(f"WARNING: {warning}")
    if result.get("warnings"):
        print()

    if result.get("from_cache"):
        print("(served from cache \u2014 use --refresh to force a live re-check)")
        print()

    if not rows:
        print("No confirmed matches found on any marketplace.")
    else:
        priced = [row["price"] for row in rows if row.get("price") is not None]
        cheapest = min(priced) if priced else None
        header = f"{'Marketplace':<20}{'Price':<15}{'Confidence':<12}Link"
        print(header)
        print("-" * len(header))
        for row in rows:
            is_cheapest = cheapest is not None and row.get("price") == cheapest and row.get("in_stock", True)
            marker = "* " if is_cheapest else "  "
            link = row.get("url") or ""
            print(f"{marker}{row['shop']:<18}{format_price(row.get('price')):<15}{_pct(row.get('confidence')):<12}{link}")
        if cheapest is not None:
            print()
            print("* = cheapest confirmed match")
        _print_comparison(gem.get("price"), cheapest)

    if result.get("possible_matches"):
        print()
        print("Possible matches (not confirmed):")
        for row in result["possible_matches"]:
            why = row.get("why") or row.get("reason") or "possible"
            print(f"  - {row.get('shop') or '?'}: {format_price(row.get('price'))} ({why})")

    if result.get("similar"):
        _print_similar(result["similar"])

    not_found_on = result.get("not_found_on") or []
    if not_found_on:
        print()
        print("Not found (searched, no confirmed match): " + ", ".join(not_found_on))

    search_issues = result.get("search_issues") or []
    if search_issues:
        print()
        print("Search issues (scrape failed - NOT a 'not sold there' verdict):")
        for issue in search_issues:
            print(f"  - {issue}")

    skipped = result.get("skipped") or []
    if skipped:
        print()
        print(f"Candidates skipped (no evidence or LLM error): {len(skipped)}")
        from urllib.parse import urlparse

        skipped_doms = sorted(
            {
                urlparse(u).netloc.lower().removeprefix("www.")
                for u in skipped
                if isinstance(u, str) and u.startswith("http") and urlparse(u).netloc
            }
        )
        if skipped_doms:
            print("  on: " + ", ".join(skipped_doms))

    trace_id = result.get("trace_id")
    if trace_id:
        print()
        print(f"Full decision trail: traces{os.sep}{trace_id}.json")
    print()


def _pct(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):.0%}"


def _print_comparison(gem_price, cheapest) -> None:
    if gem_price is None or cheapest is None:
        return
    savings = gem_price - cheapest
    if savings >= 0:
        print(
            f"Comparison: GeM {format_price(gem_price)} vs cheapest "
            f"{format_price(cheapest)} -> cheaper by {format_price(savings)}"
        )
    else:
        print(
            f"Comparison: GeM {format_price(gem_price)} vs cheapest "
            f"{format_price(cheapest)} -> GeM cheaper by {format_price(-savings)}"
        )


def _print_similar(rows) -> None:
    print()
    print("Similar (not the same product):")
    for row in rows:
        shop = row.get("source_domain") or row.get("shop") or "?"
        title = row.get("title") or "(no title)"
        url = row.get("candidate_url") or row.get("url") or ""
        print(f"  - {title} ({shop}) {format_price(row.get('price'))} {url}".rstrip())


class Printer:
    """Turns pipeline events into terminal output.

    The first block is printed when `run_finished` arrives; after that only
    the things the retry changed are printed. `clock` is injectable so a test
    can prove the wait limit without sleeping 120 seconds.
    """

    def __init__(self, gem_url: str, *, no_wait: bool = False, limit: float | None = None, clock=None):
        self.gem_url = gem_url
        self.no_wait = no_wait
        self.limit = _wait_limit() if limit is None else limit
        self.clock = clock or _clock
        self.cheapest = None
        self.gem_price = None
        self.playing = False
        self.wait_started = None
        self.stopped_waiting = False

    # -- the one callback the pipeline calls --
    def on_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "run_finished":
            result = event.get("result") or {}
            print_result(self.gem_url, result)
            self.cheapest = _cheapest_view(result)
            self.gem_price = _gem_view(result).get("price")
            pending = any(
                entry.get("retryable") for entry in (result.get("shops") or {}).values() if isinstance(entry, dict)
            ) or (not (result.get("matches") or []) and not (result.get("similar") or []))
            if pending and not self.no_wait:
                self.playing = True
                self.wait_started = self.clock()
                print(WAITING_LINE.format(limit=self.limit))
                sys.stdout.flush()
        elif kind == "shop_updated":
            self._on_shop_updated(event)
        elif kind == "similar_ready":
            if not self.no_wait:
                _print_similar(event.get("similar") or [])
        elif kind == "done":
            self.finish()

    def _over_limit(self) -> bool:
        return self.wait_started is not None and (self.clock() - self.wait_started) > self.limit

    def _on_shop_updated(self, event: dict) -> None:
        if self.no_wait or not self.playing or self.stopped_waiting:
            return
        if self._over_limit():
            print(f"Stopped waiting for updates after {self.limit:.0f} s; the first result above still stands.")
            self.stopped_waiting = True
            return
        shop = event.get("shop") or "?"
        print(f"Updated: {shop} {event.get('status') or 'unknown'} {format_price(event.get('price'))}")
        cheapest = self._shop_cheapest(event)
        if cheapest is not None and (self.cheapest is None or cheapest != self.cheapest.get("price")):
            saving = None if self.gem_price is None else self.gem_price - cheapest
            tail = "" if saving is None else f" ({format_price(abs(saving))} {'less' if saving >= 0 else 'more'} than GeM)"
            print(f"Cheapest is now {format_price(cheapest)} on {shop}{tail}")
            self.cheapest = {"shop": shop, "price": cheapest, "saving_vs_gem": saving}

    def _shop_cheapest(self, event: dict):
        return event.get("price") if event.get("status") == "found" else None

    def finish(self) -> None:
        if self.wait_started is not None and not self.stopped_waiting and self.clock() - self.wait_started > self.limit:
            print(f"Stopped waiting for updates after {self.limit:.0f} s; the first result above still stands.")
            self.stopped_waiting = True
        print("Done.")



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare a GeM product's price against other marketplaces.")
    parser.add_argument("gem_url", help="A GeM product URL (mkp.gem.gov.in/.../p-<id>-<id>-cat.html)")
    parser.add_argument("--refresh", action="store_true", help="Bypass the cache and force a live re-check")
    parser.add_argument("--no-wait", action="store_true", help="Print the first result and stop; do not wait for retries")
    parser.add_argument("--json", action="store_true", help="Print the v2 result as JSON instead of the table")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Send every candidate to the verifier for this run (GEM_STRICT=1)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Accepted for old scripts; fast is the default now, so it changes nothing",
    )
    return parser


def main(argv=None) -> int:
    # ₹ crashes on cp1252 Windows consoles (observed live:
    # UnicodeEncodeError) — force UTF-8 with replacement so the table mode
    # never dies on a currency symbol. StringIO in tests has no reconfigure.
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    if args.strict:
        # config reads GEM_STRICT at import time, so this must happen before
        # the pipeline (and core.config) is imported.
        os.environ["GEM_STRICT"] = "1"

    from core.gem_url import looks_like_gem_url

    if not looks_like_gem_url(args.gem_url):
        print("not a GeM product link", file=sys.stderr)
        return 2

    from core import config, pipeline

    config.log_startup_check()
    printer = Printer(args.gem_url, no_wait=args.no_wait or args.json)
    try:
        result = pipeline.run_pipeline(args.gem_url, refresh=args.refresh, on_event=printer.on_event)
    except Exception as e:
        print(f"Pipeline failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
