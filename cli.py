"""
Part 7 — CLI.

    python cli.py <gem_url> [--refresh] [--json]

Thin wrapper: calls run_pipeline, prints a plain-text comparison table (or
raw JSON with --json). All the actual logic lives in Parts 1-6 — this file
is just presentation and argument parsing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import config
import pipeline


def format_price(value) -> str:
    if value is None:
        return "—"
    return f"\u20b9{value:,.2f}"


def print_result(gem_url: str, result: dict) -> None:
    gem_product = result.get("gem_product") or {}
    matches = result.get("matches") or []
    not_found_on = result.get("not_found_on") or []

    print()
    print("GeM Price Comparison")
    print("=" * 60)
    print(f"Product  : {gem_product.get('title') or '(title not extracted)'}")
    print(f"GeM price: {format_price(gem_product.get('gem_price'))} ({gem_product.get('gem_price_type', 'unknown')})")
    print(f"URL      : {gem_url}")
    print()

    if result.get("from_cache"):
        print("(served from cache — use --refresh to force a live re-check)")
        print()

    if not matches:
        print("No confirmed matches found on any marketplace.")
    else:
        priced = [m["price_used"] for m in matches if m.get("price_used") is not None]
        cheapest = min(priced) if priced else None

        header = f"{'Marketplace':<20}{'Price':<15}{'Confidence':<12}Link"
        print(header)
        print("-" * len(header))
        for m in matches:
            is_cheapest = cheapest is not None and m.get("price_used") == cheapest
            marker = "* " if is_cheapest else "  "
            price_str = format_price(m.get("price_used"))
            conf_str = f"{m.get('confidence', 0):.0%}"
            domain = m.get("source_domain", "?")
            link = m.get("candidate_url", "")
            print(f"{marker}{domain:<18}{price_str:<15}{conf_str:<12}{link}")
        if cheapest is not None:
            print()
            print("* = cheapest confirmed match")

        # The actual comparison the whole project exists for.
        if gem_product.get("gem_price") is not None and cheapest is not None:
            savings = gem_product["gem_price"] - cheapest
            if savings >= 0:
                print(
                    f"Comparison: GeM {format_price(gem_product['gem_price'])} vs cheapest "
                    f"{format_price(cheapest)} -> cheaper by {format_price(savings)}"
                )
            else:
                print(
                    f"Comparison: GeM {format_price(gem_product['gem_price'])} vs cheapest "
                    f"{format_price(cheapest)} -> GeM cheaper by {format_price(-savings)}"
                )

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

    trace_id = result.get("trace_id")
    if trace_id:
        print()
        print(f"Full decision trail: traces{os.sep}{trace_id}.json")
    print()


def main() -> None:
    # ₹ crashes on cp1252 Windows consoles (observed live:
    # UnicodeEncodeError) — force UTF-8 with replacement so the table mode
    # never dies on a currency symbol. StringIO in tests has no reconfigure.
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Compare a GeM product's price against other marketplaces.")
    parser.add_argument("gem_url", help="A GeM product URL (mkp.gem.gov.in/.../p-<id>-<id>-cat.html)")
    parser.add_argument("--refresh", action="store_true", help="Bypass the cache and force a live re-check")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of the formatted table")
    args = parser.parse_args()

    config.log_startup_check()

    try:
        result = pipeline.run_pipeline(args.gem_url, refresh=args.refresh)
    except Exception as e:
        print(f"Pipeline failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_result(args.gem_url, result)


if __name__ == "__main__":
    main()
