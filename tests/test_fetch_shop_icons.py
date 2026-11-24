"""Offline tests for the shop icon collector (no network)."""
from __future__ import annotations

import json
from pathlib import Path

from tools import fetch_shop_icons


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_record_domains_counts_hosts_in_runs_traces_and_results(tmp_path, monkeypatch):
    runs = tmp_path / "cache" / "runs"
    traces = tmp_path / "cache" / "traces"
    results = tmp_path / "eval" / "results"
    _write(runs / "one.json", json.dumps({"matches": [{"url": "https://www.shop.example/p/1"}]}))
    _write(runs / "two.json", json.dumps({"shops": {"shop.example": {"status": "found"}}}))
    _write(traces / "three.json", json.dumps({"url": "http://other.example/x", "error": "See https://curl.se/libcurl/c/libcurl-errors.html"}))
    _write(results / "four.jsonl", '{"shop": "third.example"}\n')
    monkeypatch.setattr(fetch_shop_icons, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(fetch_shop_icons, "EVAL_RESULTS", results)
    found = fetch_shop_icons._record_domains()
    assert found[0] == "shop.example"
    assert set(found[1:]) == {"other.example", "third.example"}


def test_record_domains_keeps_only_the_most_frequent(tmp_path, monkeypatch):
    runs = tmp_path / "cache" / "runs"
    rows = [{"url": "https://busy.example/p/1"}] * 3 + [
        {"url": "https://rare.example/p/1"},
        {"url": "https://medium.example/p/1"},
        {"url": "https://medium.example/p/2"},
    ]
    _write(runs / "one.json", json.dumps({"matches": rows}))
    monkeypatch.setattr(fetch_shop_icons, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(fetch_shop_icons, "EVAL_RESULTS", tmp_path / "missing")
    found = fetch_shop_icons._record_domains(limit=2)
    assert found == ["busy.example", "medium.example"]


def test_stdin_domains_are_plain_lines(monkeypatch):
    class FakeStdin:
        def isatty(self):
            return False

        def read(self):
            return " One.Example \n\nsecond.example\n"

    monkeypatch.setattr(fetch_shop_icons.sys, "stdin", FakeStdin())
    assert fetch_shop_icons._stdin_domains() == ["one.example", "second.example"]


def test_stdin_is_ignored_when_interactive(monkeypatch):
    class FakeStdin:
        def isatty(self):
            return True

        def read(self):
            raise AssertionError("interactive stdin must not be read")

    monkeypatch.setattr(fetch_shop_icons.sys, "stdin", FakeStdin())
    assert fetch_shop_icons._stdin_domains() == []


def test_wanted_domains_merge_without_duplicates(tmp_path, monkeypatch):
    _write(tmp_path / "shops.txt", "shop.example\nother.example\n")
    monkeypatch.setattr(fetch_shop_icons, "SHOPS_FILE", tmp_path / "shops.txt")
    monkeypatch.setattr(fetch_shop_icons, "EXTRA_DOMAINS", ("shop.example", "flipkart.com"))
    monkeypatch.setattr(fetch_shop_icons, "CACHE_DIR", tmp_path / "missing")
    monkeypatch.setattr(fetch_shop_icons, "EVAL_RESULTS", tmp_path / "missing")

    class FakeStdin:
        def isatty(self):
            return False

        def read(self):
            return "other.example\nstdin.example\n"

    monkeypatch.setattr(fetch_shop_icons.sys, "stdin", FakeStdin())
    assert fetch_shop_icons._wanted_domains() == [
        "shop.example",
        "other.example",
        "flipkart.com",
        "stdin.example",
    ]
