from pathlib import Path

from tools import doctor


def test_the_offline_checks_find_nothing_broken(monkeypatch):
    # The autouse fixture hides curl_cffi so no test can reach the network
    # through libcurl; the package check must see the real installed module.
    import sys

    monkeypatch.delitem(sys.modules, "curl_cffi", raising=False)
    results = doctor.run_checks()
    failures = [message for status, message in results if status == doctor.FAIL]
    assert not failures, failures
    assert len(results) == len(doctor.OFFLINE_CHECKS)


def test_a_check_that_crashes_is_reported_as_a_failure_not_a_crash(monkeypatch):
    def broken():
        raise RuntimeError("oops")

    monkeypatch.setattr(doctor, "OFFLINE_CHECKS", [broken])
    results = doctor.run_checks()
    assert results[0][0] == doctor.FAIL and "oops" in results[0][1]


def test_the_demo_check_passes_and_restores_the_delay(monkeypatch):
    from api import demo

    slept = []
    monkeypatch.setattr(demo, "DEMO_DELAY", 0.5)
    monkeypatch.setattr(demo.time, "sleep", lambda s: slept.append(s))
    status, _ = doctor.check_demo_run()
    assert status == doctor.PASS
    assert slept == []  # the delay was 0 for the whole check, so it never slept
    assert demo.DEMO_DELAY == 0.5  # and the real delay is back


def test_the_llm_check_warns_when_nothing_is_usable(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDERS_FILE", "")
    from llm import router

    monkeypatch.setattr(router, "load_endpoints", lambda: ([router.Endpoint(
        provider="x", model="m", base_url="https://x.test/v1", api_key_env="K", enabled=False, disabled_reason="K is not set")], "failover"))
    status, message = doctor.check_llm_config()
    assert status == doctor.WARN and "K is not set" in message


def test_a_broken_provider_file_is_a_failure(monkeypatch):
    from llm import router

    def explode():
        raise ValueError("bad json")

    monkeypatch.setattr(router, "load_endpoints", explode)
    status, message = doctor.check_llm_config()
    assert status == doctor.FAIL and "bad json" in message


def test_failover_check_shows_a_bad_key_being_skipped(monkeypatch):
    from types import SimpleNamespace

    from llm import router

    def endpoint(provider):
        return router.Endpoint(provider=provider, model="m", base_url=f"https://{provider}.test/v1", api_key_env=f"{provider.upper()}_KEY")

    class FakeHTTPError(OSError):
        status_code = 401
        response = SimpleNamespace(headers={})

    def fake_client(self, ep):
        rejected = __import__("os").environ.get(ep.api_key_env) == doctor.BAD_KEY

        def create(**kwargs):
            if rejected:
                raise FakeHTTPError("bad key")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))])

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(router, "load_endpoints", lambda: ([endpoint("one"), endpoint("two")], "failover"))
    monkeypatch.setattr(router.Router, "_make_client", fake_client)
    monkeypatch.setenv("ONE_KEY", "real-one")

    [(status, message)] = doctor.check_live_failover()
    assert status == doctor.PASS and "one/m was rejected" in message and "two/m answered" in message
    assert __import__("os").environ["ONE_KEY"] == "real-one"  # the real key is put back


def test_failover_check_is_skipped_with_a_single_endpoint(monkeypatch):
    from llm import router

    only = router.Endpoint(provider="one", model="m", base_url="https://one.test/v1", api_key_env="K")
    monkeypatch.setattr(router, "load_endpoints", lambda: ([only], "failover"))
    [(status, message)] = doctor.check_live_failover()
    assert status == doctor.WARN and "two usable endpoints" in message


def test_the_engine_canary_reports_each_engine(monkeypatch):
    from discovery import discover

    monkeypatch.setattr(discover, "_active_engines", lambda: {
        "one": lambda query, session: (["https://a.test/x"], False),
        "two": lambda query, session: ([], True),
        "three": lambda query, session: ([], False),
    })
    rows = doctor.check_live_engines()
    statuses = [status for status, _ in rows]
    assert statuses == [doctor.PASS, doctor.WARN, doctor.WARN]
    assert "1 link" in rows[0][1]


def test_a_crashing_engine_is_a_failure_not_a_crash(monkeypatch):
    from discovery import discover

    def explode(query, session):
        raise RuntimeError("engine gone")

    monkeypatch.setattr(discover, "_active_engines", lambda: {"bad": explode})
    [(status, message)] = doctor.check_live_engines()
    assert status == doctor.FAIL and "engine gone" in message


def test_the_shop_canary_passes_when_a_card_parses(monkeypatch):
    from scrapers import amazon, flipkart
    from scrapers._base import CandidateResult, SearchScrapeResult

    card = CandidateResult(source_domain="shop.test", url="https://shop.test/x", scraper_used="test",
                           structured_fields={"title": "Cable", "price": "399"}, raw_page_text=None)
    found = SearchScrapeResult(source_domain="shop.test", query="q", candidates=[card])
    monkeypatch.setattr(flipkart, "search", lambda query, **kw: found)
    monkeypatch.setattr(amazon, "search", lambda query, **kw: found)
    rows = doctor.check_live_shops()
    assert [status for status, _ in rows] == [doctor.PASS, doctor.PASS]


def test_a_failed_canary_saves_the_page_and_names_it(monkeypatch, tmp_path):
    from scrapers import amazon, flipkart
    from scrapers._base import SearchScrapeResult

    empty = SearchScrapeResult(source_domain="shop.test", query="q", candidates=[], blocked=True)
    monkeypatch.setattr(flipkart, "search", lambda query, **kw: empty)
    monkeypatch.setattr(amazon, "search", lambda query, **kw: empty)
    monkeypatch.setattr(doctor, "DEBUG_HTML_DIR", tmp_path)
    monkeypatch.setattr(doctor, "_canary_page_html", lambda shop, query: "<html>blocked</html>")
    rows = doctor.check_live_shops()
    assert [status for status, _ in rows] == [doctor.WARN, doctor.WARN]
    saved = sorted(tmp_path.glob("*.html"))
    assert len(saved) == 2 and "blocked" in saved[0].read_text(encoding="utf-8")
    for _, message in rows:
        assert "page saved: " in message
        assert Path(message.split("page saved: ")[1].rstrip(")")).exists()


def test_clean_removes_only_old_files(monkeypatch, tmp_path):
    import os
    import time

    from core import pipeline, runs

    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    old.write_text("{}", encoding="utf-8")
    new.write_text("{}", encoding="utf-8")
    stamp = time.time() - 40 * 86400
    os.utime(old, (stamp, stamp))
    monkeypatch.setattr(pipeline, "TRACES_DIR", tmp_path)
    monkeypatch.setattr(runs, "runs_dir", lambda: tmp_path / "no-runs-here")
    monkeypatch.setattr(doctor, "DEBUG_HTML_DIR", tmp_path / "no-debug-here")

    rows = doctor.clean_old_files(30)
    assert not old.exists() and new.exists()
    assert rows[0][0] == doctor.PASS and "removed 1 old trace" in rows[0][1]


def test_live_is_the_umbrella_for_every_live_check(monkeypatch):
    import sys

    seen = {}

    def fake_run_checks(**kwargs):
        seen.update(kwargs)
        return [(doctor.PASS, "ok")]

    monkeypatch.setattr(doctor, "run_checks", fake_run_checks)
    monkeypatch.setattr(sys, "argv", ["doctor.py", "--live"])
    assert doctor.main() == 0
    assert all(seen[name] for name in ("live_llm", "live_engines", "live_shops", "live_failover", "live_scrape"))


def test_a_live_check_skips_a_retired_model_with_a_warning(monkeypatch):
    import json
    import time

    from core import config
    from llm import router

    endpoint = router.Endpoint(provider="nvidia", model="nvidia/gone", base_url="https://x.test/v1", api_key_env="K")
    monkeypatch.setattr(router, "load_endpoints", lambda: ([endpoint], "failover"))
    state = Path(config.CACHE_DIR) / "llm_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({
        "used_up": {},
        "retired": {"nvidia/gone": {"at": time.time(), "reason": "model not found (404)"}},
    }), encoding="utf-8")

    [(status, message)] = doctor.check_live_llm()
    assert status == doctor.WARN
    assert "skipped" in message and "404" in message


def test_the_live_check_gives_a_reasoning_model_room(monkeypatch):
    from llm import router

    seen = {}

    class _FakeRouter:
        def __init__(self, endpoints, **kwargs):
            self.endpoints = endpoints

        def complete(self, messages, max_tokens, purpose=""):
            seen["max_tokens"] = max_tokens
            return '{"ok": true}'

    endpoint = router.Endpoint(provider="x", model="m", base_url="https://x.test/v1", api_key_env="K")
    monkeypatch.setattr(router, "Router", _FakeRouter)
    monkeypatch.setattr(router, "load_endpoints", lambda: ([endpoint], "failover"))

    [(status, _)] = doctor.check_live_llm()
    assert status == doctor.PASS
    assert seen["max_tokens"] == 256
