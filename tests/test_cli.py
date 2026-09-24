"""
Tests for cli.py's formatting logic — format_price and print_result.
Captures stdout via contextlib.redirect_stdout rather than pytest's capsys
fixture, so this file still runs standalone via its __main__ block too,
same as every other test file in this project.
"""
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cli  # noqa: E402


# --- format_price --------------------------------------------------------------------

def test_format_price_none():
    assert cli.format_price(None) == "\u2014"


def test_format_price_formats_with_commas_and_two_decimals():
    assert cli.format_price(129900) == "\u20b9129,900.00"


def test_format_price_small_value():
    assert cli.format_price(999) == "\u20b9999.00"


# --- print_result ----------------------------------------------------------------------

def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


def test_print_result_marks_cheapest_match():
    result = {
        "gem_product": {"title": "Logitech M650", "gem_price": 1299.0, "gem_price_type": "fixed"},
        "matches": [
            {"source_domain": "amazon.in", "price_used": 1249.0, "confidence": 0.9, "candidate_url": "https://amazon.in/x"},
            {"source_domain": "flipkart.com", "price_used": 1199.0, "confidence": 0.95, "candidate_url": "https://flipkart.com/y"},
        ],
        "not_found_on": [],
        "from_cache": False,
        "trace_id": "abc123",
    }
    output = _capture(cli.print_result, "https://mkp.gem.gov.in/x", result)

    flipkart_line = next(line for line in output.splitlines() if "flipkart.com" in line)
    amazon_line = next(line for line in output.splitlines() if "amazon.in" in line and "not found" not in line)
    assert flipkart_line.startswith("*")  # cheaper one gets the marker
    assert not amazon_line.startswith("*")
    assert "abc123" in output  # trace id surfaced


def test_print_result_no_matches():
    result = {
        "gem_product": {"title": "Obscure Item", "gem_price": None, "gem_price_type": "unknown"},
        "matches": [],
        "not_found_on": ["amazon.in", "flipkart.com"],
        "from_cache": False,
        "trace_id": "def456",
    }
    output = _capture(cli.print_result, "https://mkp.gem.gov.in/x", result)
    assert "No confirmed matches" in output
    assert "amazon.in" in output and "flipkart.com" in output


def test_print_result_shows_cache_notice():
    result = {
        "gem_product": {"title": "X", "gem_price": 100.0, "gem_price_type": "fixed"},
        "matches": [],
        "not_found_on": [],
        "from_cache": True,
        "trace_id": "ghi789",
    }
    output = _capture(cli.print_result, "https://mkp.gem.gov.in/x", result)
    assert "served from cache" in output


def test_print_result_handles_match_with_no_price():
    # a confirmed match where the LLM couldn't pin down a price shouldn't
    # crash formatting or be wrongly marked "cheapest" (matcher now demotes
    # these to skipped for live runs, but cached/legacy results can still
    # carry one)
    result = {
        "gem_product": {"title": "X", "gem_price": 100.0, "gem_price_type": "fixed"},
        "matches": [{"source_domain": "brand.example.com", "price_used": None, "confidence": 0.8, "candidate_url": "https://brand.example.com/x"}],
        "not_found_on": [],
        "from_cache": False,
        "trace_id": "jkl012",
    }
    output = _capture(cli.print_result, "https://mkp.gem.gov.in/x", result)
    assert "—" in output  # the em-dash placeholder for a missing price
    assert "* =" not in output  # nothing to mark as cheapest when no price exists


def test_print_result_shows_warnings():
    result = {
        "gem_product": {"title": "X", "gem_price": None, "gem_price_type": "unknown"},
        "matches": [],
        "not_found_on": [],
        "warnings": ["GeM price not extracted — price-sanity gate is OFF and savings comparison unavailable."],
        "from_cache": False,
        "trace_id": "warn01",
    }
    output = _capture(cli.print_result, "https://mkp.gem.gov.in/x", result)
    assert "WARNING" in output
    assert "sanity gate" in output


def test_print_result_shows_skipped_domains():
    result = {
        "gem_product": {"title": "X", "gem_price": 100.0, "gem_price_type": "fixed"},
        "matches": [],
        "not_found_on": [],
        "skipped": ["https://www.amazon.in/dp/B0SKIP"],
        "from_cache": False,
        "trace_id": "skip01",
    }
    output = _capture(cli.print_result, "https://mkp.gem.gov.in/x", result)
    assert "amazon.in" in output  # which domain(s) the skips hit, not just a count


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
