"""Discovery asks every engine at once and never lets one engine fail a run.

A dead engine is recorded, a slow one is dropped at the deadline, an engine
that keeps failing in this process is skipped for five minutes, and a shop
listed in discovery/indian_shops.txt counts as Indian even without a .in.
"""
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import discovery  # noqa: E402


@contextmanager
def _patched(obj, **attrs):
    old = {name: getattr(obj, name) for name in attrs}
    for name, value in attrs.items():
        setattr(obj, name, value)
    try:
        yield
    finally:
        for name, value in old.items():
            setattr(obj, name, value)


@pytest.fixture(autouse=True)
def _fresh_engine_health(monkeypatch):
    """Engine health is process state; every test starts from zero."""
    monkeypatch.setattr(discovery.discover, "_engine_failures", {})
    monkeypatch.setattr(discovery.discover, "_engine_skip_until", {})


def _engine(urls, blocked=False, delay=0.0):
    def run(query, session):
        if delay:
            time.sleep(delay)
        if isinstance(urls, Exception):
            raise urls
        return list(urls), blocked
    return run


CROMA = "https://www.croma.com/logitech-m650/p/12345"
RELIANCE = "https://www.reliancedigital.in/logitech-m650"


def test_engines_run_together_and_their_results_union(monkeypatch):
    monkeypatch.setattr(discovery.discover, "ENGINE_FUNCS", {
        "one": _engine([CROMA]),
        "two": _engine([RELIANCE]),
    })
    result = discovery.discover_candidates("logitech m650", ["croma.com", "reliancedigital.in"])
    assert result.filtered_urls == [CROMA, RELIANCE]
    assert result.search_issues == []


def test_duplicate_listings_from_two_engines_collapse(monkeypatch):
    monkeypatch.setattr(discovery.discover, "ENGINE_FUNCS", {
        "one": _engine([f"{CROMA}?utm_source=one"]),
        "two": _engine([f"{CROMA}?utm_source=two"]),
    })
    result = discovery.discover_candidates("logitech m650", ["croma.com"])
    assert len(result.raw_urls) == 1


def test_a_failing_engine_is_recorded_and_the_others_are_used(monkeypatch):
    monkeypatch.setattr(discovery.discover, "ENGINE_FUNCS", {
        "broken": _engine(ConnectionError("backend down")),
        "working": _engine([CROMA]),
    })
    result = discovery.discover_candidates("logitech m650", ["croma.com"])
    assert result.filtered_urls == [CROMA]
    assert any("backend down" in issue for issue in result.search_issues)


def test_a_slow_engine_is_dropped_at_the_time_limit(monkeypatch):
    monkeypatch.setattr(discovery.discover, "ENGINE_FUNCS", {
        "slow": _engine([RELIANCE], delay=1.0),
        "fast": _engine([CROMA]),
    })
    monkeypatch.setattr(discovery.discover, "DISCOVERY_TIME_LIMIT_SECONDS", 0.3)
    started = time.monotonic()
    result = discovery.discover_candidates("logitech m650", ["croma.com", "reliancedigital.in"])
    elapsed = time.monotonic() - started
    assert elapsed < 0.9
    assert result.filtered_urls == [CROMA]  # the slow engine's result never arrived
    assert any("time limit" in issue for issue in result.search_issues)


def test_a_blocked_engine_counts_as_blocked_only_when_nothing_came_back(monkeypatch):
    monkeypatch.setattr(discovery.discover, "ENGINE_FUNCS", {
        "blocked": _engine([], blocked=True),
    })
    result = discovery.discover_candidates("logitech m650", ["croma.com"])
    assert result.blocked is True
    assert result.filtered_urls == []


# --- engine health memory -------------------------------------------------------------

def _failing_engine_map():
    return {"broken": _engine(ConnectionError("down"))}


def test_three_failures_skip_the_engine_for_five_minutes(monkeypatch):
    monkeypatch.setattr(discovery.discover, "ENGINE_FUNCS", _failing_engine_map())
    calls = []
    real_engine = discovery.discover.ENGINE_FUNCS["broken"]

    def counting(query, session):
        calls.append(query)
        return real_engine(query, session)

    monkeypatch.setattr(discovery.discover, "ENGINE_FUNCS", {"broken": counting})
    for _ in range(3):
        discovery.discover_candidates("q", ["croma.com"])
    assert len(calls) == 3
    assert discovery.discover._engine_skip_until["broken"] > time.time() + 200  # ~5 minutes

    result = discovery.discover_candidates("q", ["croma.com"])
    assert len(calls) == 3  # skipped now, engine not called again
    assert any("skipped" in issue for issue in result.search_issues)


def test_the_skip_expires(monkeypatch):
    monkeypatch.setattr(discovery.discover, "_engine_skip_until", {"broken": time.time() - 1.0})
    assert discovery.discover._engine_skipped("broken") is False

    monkeypatch.setattr(discovery.discover, "_engine_skip_until", {"broken": time.time() + 60.0})
    assert discovery.discover._engine_skipped("broken") is True


def test_a_success_resets_the_failure_count(monkeypatch):
    calls = {"n": 0}

    def flaky(query, session):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ConnectionError("down")
        return [CROMA], False

    monkeypatch.setattr(discovery.discover, "ENGINE_FUNCS", {"flaky": flaky})
    discovery.discover_candidates("q", ["croma.com"])
    discovery.discover_candidates("q", ["croma.com"])
    assert discovery.discover._engine_failures["flaky"] == 2

    discovery.discover_candidates("q", ["croma.com"])
    assert discovery.discover._engine_failures["flaky"] == 0


# --- Indian shop allow-list file ------------------------------------------------------

def test_a_shop_from_the_allowlist_file_counts_as_indian():
    assert discovery.discover._is_indian_retail("boat-lifestyle.com") is True
    assert discovery.discover._is_indian_retail("www.boat-lifestyle.com") is True
    assert discovery.discover._is_indian_retail("example.com") is False


def test_the_allowlist_file_is_comments_and_domains_only():
    path = Path(discovery.discover._INDIAN_SHOP_ALLOWLIST_FILE)
    assert path.exists()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        assert not line or line.startswith("#") or "." in line


# --- the optional ddgs engine ---------------------------------------------------------

def test_ddgs_engine_is_skipped_when_not_installed(monkeypatch):
    monkeypatch.setattr(discovery.discover, "_ddgs_installed", False)
    assert "ddgs" not in discovery.discover._active_engines()


def test_ddgs_engine_reads_rows_from_the_package(monkeypatch):
    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, max_results=10):
            return [
                {"href": CROMA, "title": "Croma"},
                {"url": RELIANCE, "title": "Reliance"},
                {"title": "no link here"},
            ]

    ddgs_stub = type(sys)("ddgs")
    ddgs_stub.DDGS = _FakeDDGS
    monkeypatch.setitem(sys.modules, "ddgs", ddgs_stub)
    urls, blocked = discovery.discover._engine_ddgs("logitech m650", None)
    assert urls == [CROMA, RELIANCE]
    assert blocked is False
