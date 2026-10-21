"""
Tests for cli.py: the printed blocks, the flag handling and the wait limit.

The pipeline is faked here; the printer tests feed events directly, so the
wait limit can be proven with a fake clock instead of sleeping 120 seconds.
"""
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cli  # noqa: E402

GEM_URL = "https://mkp.gem.gov.in/computer-mouse/demo/p-500-600-cat.html"


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


def _v2_result(**overrides):
    result = {
        "result_version": 2,
        "state": "done",
        "trace_id": "abc123",
        "from_cache": False,
        "gem": {"title": "Logitech M650", "price": {"low": 1299.0, "high": 1299.0, "kind": "fixed"}},
        "shops": {
            "amazon.in": {"status": "found", "reason": "", "seen": 2, "dropped": 0, "checked": 2,
                          "matches": 1, "price": 1249.0, "retried": False, "retryable": False, "seconds": 4.0},
            "flipkart.com": {"status": "found", "reason": "", "seen": 3, "dropped": 1, "checked": 2,
                             "matches": 1, "price": 1199.0, "retried": True, "retryable": True, "seconds": 6.0},
        },
        "matches": [
            {"shop": "flipkart.com", "price": 1199.0, "confidence": 0.95, "reason": "same model",
             "url": "https://flipkart.com/y", "in_stock": True},
            {"shop": "amazon.in", "price": 1249.0, "confidence": 0.9, "reason": "same model",
             "url": "https://amazon.in/x", "in_stock": True},
        ],
        "possible_matches": [],
        "similar": [],
        "cheapest": {"shop": "flipkart.com", "price": 1199.0, "saving_vs_gem": 100.0},
        "warnings": [],
        "not_found_on": [],
        "search_issues": [],
        "skipped": [],
        "comparison": {"gem_price": 1299.0, "cheapest_match_price": 1199.0, "savings_vs_gem": 100.0},
    }
    result.update(overrides)
    return result


# --- format_price --------------------------------------------------------------------

def test_format_price_none():
    assert cli.format_price(None) == "\u2014"


def test_format_price_formats_with_commas_and_two_decimals():
    assert cli.format_price(129900) == "\u20b9129,900.00"


def test_format_price_small_value():
    assert cli.format_price(999) == "\u20b9999.00"


def test_format_price_accepts_a_scraped_string():
    # Generic scrapes keep the page's price string; a similar-product row
    # could therefore hold '₹51,313.00' and crash the :,.2f format.
    assert cli.format_price("\u20b951,313.00") == "\u20b951,313.00"
    assert cli.format_price("Rs. 1,299") == "\u20b91,299.00"


def test_format_price_leaves_an_unparseable_text_alone():
    assert cli.format_price("Call for price") == "Call for price"


# --- print_result --------------------------------------------------------------------

def test_print_result_marks_cheapest_match():
    output = _capture(cli.print_result, GEM_URL, _v2_result())

    flipkart_line = next(line for line in output.splitlines() if "flipkart.com" in line and line.startswith("*"))
    amazon_line = next(line for line in output.splitlines() if "amazon.in" in line and line.strip().startswith("amazon.in"))
    assert not amazon_line.startswith("*")
    assert "abc123" in output


def test_print_result_prints_the_cheapest_line():
    output = _capture(cli.print_result, GEM_URL, _v2_result())
    assert "Cheapest : \u20b91,199.00 on flipkart.com" in output
    assert "\u20b9100.00 less than GeM" in output


def test_print_result_says_when_gem_is_cheaper():
    result = _v2_result(cheapest={"shop": "amazon.in", "price": 1399.0, "saving_vs_gem": -100.0})
    output = _capture(cli.print_result, GEM_URL, result)
    assert "\u20b9100.00 more than GeM" in output


def test_print_result_no_matches():
    result = _v2_result(
        matches=[],
        cheapest=None,
        comparison={"gem_price": 1299.0, "cheapest_match_price": None, "savings_vs_gem": None},
        not_found_on=["amazon.in", "flipkart.com"],
    )
    output = _capture(cli.print_result, GEM_URL, result)
    assert "No confirmed matches" in output
    assert "Cheapest : not found yet" in output
    assert "amazon.in" in output and "flipkart.com" in output


def test_print_result_shows_cache_notice():
    output = _capture(cli.print_result, GEM_URL, _v2_result(from_cache=True, matches=[]))
    assert "served from cache" in output


def test_print_result_handles_match_with_no_price():
    result = _v2_result(
        matches=[{"shop": "brand.example.com", "price": None, "confidence": 0.8, "url": "https://brand.example.com/x"}],
        cheapest=None,
    )
    output = _capture(cli.print_result, GEM_URL, result)
    assert "\u2014" in output
    assert "* =" not in output


def test_print_result_shows_warnings():
    output = _capture(cli.print_result, GEM_URL, _v2_result(warnings=["GeM price not extracted"]))
    assert "WARNING" in output and "not extracted" in output


def test_print_result_shows_skipped_domains():
    result = _v2_result(skipped=["https://www.amazon.in/dp/B0SKIP"])
    output = _capture(cli.print_result, GEM_URL, result)
    assert "amazon.in" in output


def test_print_result_still_reads_an_old_v1_result():
    old = {
        "gem_product": {"title": "Old", "gem_price": 1000.0, "gem_price_type": "fixed"},
        "matches": [{"source_domain": "amazon.in", "price_used": 900.0, "confidence": 0.9,
                     "candidate_url": "https://amazon.in/x"}],
        "comparison": {"gem_price": 1000.0, "cheapest_match_price": 900.0, "savings_vs_gem": 100.0},
        "not_found_on": [],
        "from_cache": False,
        "trace_id": "old1",
    }
    output = _capture(cli.print_result, GEM_URL, old)
    assert "\u20b9900.00 on amazon.in" in output
    assert "old1" in output


def test_print_result_shows_possible_matches_and_similar():
    result = _v2_result(
        matches=[],
        cheapest=None,
        possible_matches=[{"shop": "amazon.in", "price": 1200.0, "why": "confidence 0.70 below 0.75"}],
        similar=[{"source_domain": "brand.test", "title": "M650 L", "price": 999.0, "candidate_url": "https://brand.test/x"}],
    )
    output = _capture(cli.print_result, GEM_URL, result)
    assert "Possible matches (not confirmed)" in output
    assert "Similar (not the same product)" in output
    assert "M650 L" in output


# --- Printer: the first block, the waiting line and the updates ----------------------

class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _printer(**kwargs):
    clock = _FakeClock()
    return cli.Printer(GEM_URL, clock=clock, **kwargs), clock


def test_the_first_result_is_printed_when_it_arrives():
    printer, _ = _printer()
    output = _capture(printer.on_event, {"type": "run_finished", "result": _v2_result(state="shown")})
    assert "GeM Price Comparison" in output
    assert "Cheapest : \u20b91,199.00 on flipkart.com" in output


def test_the_waiting_line_is_printed_only_when_a_retry_is_pending():
    printer, _ = _printer()
    output = _capture(printer.on_event, {"type": "run_finished", "result": _v2_result(state="shown")})
    assert "Waiting for retries (up to 120 s" in output

    found = _v2_result(state="shown")
    found["shops"]["flipkart.com"]["retryable"] = False
    quiet, _ = _printer()
    output = _capture(quiet.on_event, {"type": "run_finished", "result": found})
    assert "Waiting for retries" not in output


def test_no_wait_never_prints_the_waiting_line_or_updates():
    printer, _ = _printer(no_wait=True)
    output = _capture(printer.on_event, {"type": "run_finished", "result": _v2_result(state="shown")})
    output += _capture(printer.on_event, {"type": "shop_updated", "shop": "flipkart.com", "status": "found", "price": 1149.0})
    assert "Waiting for retries" not in output
    assert "Updated:" not in output


def test_a_shop_update_prints_the_price_and_the_new_cheapest():
    printer, clock = _printer()
    _capture(printer.on_event, {"type": "run_finished", "result": _v2_result(state="shown")})
    output = _capture(printer.on_event, {"type": "shop_updated", "shop": "flipkart.com", "status": "found", "price": 1149.0})
    assert "Updated: flipkart.com found \u20b91,149.00" in output
    assert "Cheapest is now \u20b91,149.00 on flipkart.com (\u20b9150.00 less than GeM)" in output


def test_a_shop_update_that_does_not_change_the_cheapest_prints_no_new_cheapest_line():
    printer, _ = _printer()
    _capture(printer.on_event, {"type": "run_finished", "result": _v2_result(state="shown")})
    output = _capture(printer.on_event, {"type": "shop_updated", "shop": "amazon.in", "status": "found", "price": 1199.0})
    assert "Updated: amazon.in" in output
    assert "Cheapest is now" not in output


def test_the_wait_limit_stops_the_updates_using_a_fake_clock():
    printer, clock = _printer(limit=5.0)
    _capture(printer.on_event, {"type": "run_finished", "result": _v2_result(state="shown")})
    clock.now = 6.0
    output = _capture(printer.on_event, {"type": "shop_updated", "shop": "flipkart.com", "status": "found", "price": 1.0})
    assert "Stopped waiting for updates after 5 s" in output
    assert "Updated:" not in output

    output = _capture(printer.on_event, {"type": "shop_updated", "shop": "amazon.in", "status": "found", "price": 1.0})
    assert output == ""  # silence after the limit, not a second notice


def test_similar_and_done_are_printed_at_the_end():
    printer, _ = _printer()
    _capture(printer.on_event, {"type": "run_finished", "result": _v2_result(state="shown")})
    output = _capture(printer.on_event, {"type": "similar_ready",
                                         "similar": [{"source_domain": "brand.test", "title": "M650 L", "price": 999.0}]})
    output += _capture(printer.on_event, {"type": "done"})
    assert "Similar (not the same product)" in output
    assert output.strip().endswith("Done.")


def test_the_retry_cli_wait_setting_is_read(monkeypatch):
    monkeypatch.setenv("GEM_RETRY_CLI_WAIT", "7")
    assert cli._wait_limit() == 7.0
    monkeypatch.setenv("GEM_RETRY_CLI_WAIT", "not a number")
    assert cli._wait_limit() == cli.DEFAULT_RETRY_WAIT_SECONDS


# --- main(): flags and exit codes ----------------------------------------------------

def _run_main(argv, monkeypatch, runner):
    from core import pipeline

    monkeypatch.setattr(pipeline, "run_pipeline", runner)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_a_link_that_is_not_gem_exits_2_with_a_clear_message(monkeypatch):
    called = []
    code, out, err = _run_main(["https://example.com/x"], monkeypatch, lambda *a, **k: called.append(1))
    assert code == 2
    assert "not a GeM product link" in err
    assert called == []


def test_a_failed_run_exits_1(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("the GeM page could not be read")

    code, _, err = _run_main([GEM_URL], monkeypatch, explode)
    assert code == 1
    assert "Pipeline failed" in err and "could not be read" in err


def test_json_prints_the_v2_result(monkeypatch):
    result = _v2_result()
    code, out, _ = _run_main([GEM_URL, "--json"], monkeypatch, lambda *a, **k: result)
    assert code == 0
    assert json.loads(out)["result_version"] == 2


def test_no_wait_passes_no_waiting_output(monkeypatch):
    def runner(url, refresh, on_event):
        on_event({"type": "run_finished", "result": _v2_result(state="shown")})
        on_event({"type": "shop_updated", "shop": "flipkart.com", "status": "found", "price": 1149.0})
        on_event({"type": "done"})
        return _v2_result()

    code, out, _ = _run_main([GEM_URL, "--no-wait"], monkeypatch, runner)
    assert code == 0
    assert "Waiting for retries" not in out
    assert "Updated:" not in out
    assert "Done." in out


def test_strict_is_set_before_the_pipeline_runs(monkeypatch):
    monkeypatch.delenv("GEM_STRICT", raising=False)
    seen = {}

    def runner(*args, **kwargs):
        seen["strict"] = os.environ.get("GEM_STRICT")
        return _v2_result()

    try:
        _run_main([GEM_URL, "--strict"], monkeypatch, runner)
    finally:
        os.environ.pop("GEM_STRICT", None)
    assert seen["strict"] == "1"


def test_fast_is_still_accepted(monkeypatch):
    code, _, err = _run_main([GEM_URL, "--fast"], monkeypatch, lambda *a, **k: _v2_result())
    assert code == 0 and err == ""


def test_the_refresh_flag_reaches_the_pipeline(monkeypatch):
    seen = {}

    def runner(url, refresh, on_event):
        seen["refresh"] = refresh
        return _v2_result()

    _run_main([GEM_URL, "--refresh"], monkeypatch, runner)
    assert seen["refresh"] is True


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
