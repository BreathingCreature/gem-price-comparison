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


def test_a_missing_package_names_what_to_install(monkeypatch):
    monkeypatch.setattr(doctor, "REQUIRED_PACKAGES", ["definitely_not_installed_xyz"])
    status, message = doctor.check_packages()
    assert status == doctor.FAIL
    assert "definitely_not_installed_xyz" in message and "pip install -r requirements.txt" in message


def test_a_module_that_cannot_import_is_a_failure(monkeypatch):
    def explode(name):
        raise ImportError("bad module")

    monkeypatch.setattr(doctor.importlib, "import_module", explode)
    status, message = doctor.check_modules()
    assert status == doctor.FAIL
    assert "cannot import" in message and "api.server" in message


def test_one_usable_endpoint_warns_about_missing_failover(monkeypatch):
    from llm import router

    endpoint = router.Endpoint(provider="x", model="m", base_url="https://x.test/v1", api_key_env="K")
    monkeypatch.setattr(router, "load_endpoints", lambda: ([endpoint], "spread"))
    status, message = doctor.check_llm_config()
    assert status == doctor.WARN and "only one usable endpoint" in message


def test_an_unwritable_cache_folder_is_a_failure(monkeypatch, tmp_path):
    from core import pipeline

    blocked = tmp_path / "blocked"
    blocked.write_text("not a folder", encoding="utf-8")
    monkeypatch.setattr(pipeline, "CACHE_DIR", blocked)
    status, message = doctor.check_folders()
    assert status == doctor.FAIL and "cannot write to" in message


def test_a_missing_frontend_file_is_a_failure(monkeypatch):
    monkeypatch.setattr(doctor, "FRONTEND_FILES", ["missing-page.html"])
    status, message = doctor.check_frontend_files()
    assert status == doctor.FAIL and "missing-page.html" in message


def test_a_crashing_demo_is_a_failure_and_the_delay_comes_back(monkeypatch):
    from api import demo

    monkeypatch.setattr(demo, "DEMO_DELAY", 0.25)
    monkeypatch.setattr(demo, "demo_runner", lambda *a: (_ for _ in ()).throw(RuntimeError("demo broke")))
    status, message = doctor.check_demo_run()
    assert status == doctor.FAIL and "demo broke" in message
    assert demo.DEMO_DELAY == 0.25


def test_a_demo_without_matches_or_events_is_a_failure(monkeypatch):
    from api import demo

    monkeypatch.setattr(demo, "demo_runner", lambda *a: {"matches": []})
    status, message = doctor.check_demo_run()
    assert status == doctor.FAIL
    assert "missing events" in message and "matches" in message


def test_a_broken_provider_file_stops_the_live_llm_check(monkeypatch):
    from llm import router

    monkeypatch.setattr(router, "load_endpoints", lambda: (_ for _ in ()).throw(ValueError("bad json")))
    [(status, message)] = doctor.check_live_llm()
    assert status == doctor.FAIL and "config broken" in message and "bad json" in message


def test_the_live_llm_check_warns_when_every_endpoint_is_disabled(monkeypatch):
    from llm import router

    endpoint = router.Endpoint(provider="x", model="m", base_url="https://x.test/v1", api_key_env="K", enabled=False)
    monkeypatch.setattr(router, "load_endpoints", lambda: ([endpoint], "failover"))
    [(status, message)] = doctor.check_live_llm()
    assert status == doctor.WARN and message == "no enabled endpoint to test"


def test_a_failing_endpoint_is_reported_by_the_live_llm_check(monkeypatch):
    from llm import router

    class FailingRouter:
        def __init__(self, endpoints, **kwargs):
            self.endpoints = endpoints

        def complete(self, messages, max_tokens, purpose=""):
            raise RuntimeError("quota gone")

    endpoint = router.Endpoint(provider="x", model="m", base_url="https://x.test/v1", api_key_env="K")
    monkeypatch.setattr(router, "Router", FailingRouter)
    monkeypatch.setattr(router, "load_endpoints", lambda: ([endpoint], "failover"))

    [(status, message)] = doctor.check_live_llm()
    assert status == doctor.FAIL and "quota gone" in message


def test_the_engine_check_warns_when_every_engine_is_resting(monkeypatch):
    from discovery import discover

    monkeypatch.setattr(discover, "_active_engines", lambda: {})
    [(status, message)] = doctor.check_live_engines()
    assert status == doctor.WARN and "no discovery engine" in message


def test_the_engine_check_reports_a_broken_registry(monkeypatch):
    from discovery import discover

    monkeypatch.setattr(discover, "_active_engines", lambda: (_ for _ in ()).throw(RuntimeError("registry gone")))
    [(status, message)] = doctor.check_live_engines()
    assert status == doctor.FAIL and "registry gone" in message


def test_the_canary_page_helper_reads_each_shop_and_survives_an_error(monkeypatch):
    from scrapers import amazon, flipkart

    monkeypatch.setattr(flipkart, "_fetch_light_page", lambda query: f"<html>{query}</html>")
    monkeypatch.setattr(amazon, "_fetch_curl_cffi_page", lambda query: f"<html>{query}</html>")
    assert doctor._canary_page_html("flipkart", "usb") == "<html>usb</html>"
    assert doctor._canary_page_html("amazon", "usb") == "<html>usb</html>"

    monkeypatch.setattr(amazon, "_fetch_curl_cffi_page", lambda query: (_ for _ in ()).throw(OSError("down")))
    assert doctor._canary_page_html("amazon", "usb") is None


def test_a_saved_canary_page_is_named_and_written(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "DEBUG_HTML_DIR", tmp_path)
    path = doctor._save_canary_page("flipkart", "USB C Cable", "<html>blocked</html>")
    assert path.parent == tmp_path and path.name.startswith("flipkart-usb-c-cable-")
    assert path.read_text(encoding="utf-8") == "<html>blocked</html>"


def test_the_shop_canary_reports_a_crash_and_an_empty_parse(monkeypatch, tmp_path):
    from scrapers import amazon, flipkart
    from scrapers._base import SearchScrapeResult

    monkeypatch.setattr(doctor, "_save_canary_page", lambda shop, query, html: tmp_path / f"{shop}.html")
    monkeypatch.setattr(doctor, "_canary_page_html", lambda shop, query: "")
    monkeypatch.setattr(flipkart, "search", lambda query: (_ for _ in ()).throw(RuntimeError("shop down")))
    monkeypatch.setattr(amazon, "search", lambda query: SearchScrapeResult(source_domain="amazon.in", query=query, candidates=[]))

    rows = doctor.check_live_shops()

    assert [status for status, _ in rows] == [doctor.FAIL, doctor.WARN]
    assert "canary error" in rows[0][1] and "parsed 0 cards" in rows[1][1]


def test_the_scrape_check_reports_a_crash_and_a_block(monkeypatch):
    from scrapers import amazon, flipkart
    from scrapers._base import SearchScrapeResult

    blocked = SearchScrapeResult(source_domain="amazon.in", query="q", candidates=[], blocked=True)
    monkeypatch.setattr(flipkart, "search", lambda query: (_ for _ in ()).throw(RuntimeError("no driver")))
    monkeypatch.setattr(amazon, "search", lambda query: blocked)

    rows = doctor.check_live_scrape()

    assert [status for status, _ in rows] == [doctor.FAIL, doctor.WARN]
    assert "search crashed" in rows[0][1] and "blocked the search" in rows[1][1]


def test_the_scrape_check_reports_an_error_and_an_empty_result(monkeypatch):
    from scrapers import amazon, flipkart
    from scrapers._base import SearchScrapeResult

    failed = SearchScrapeResult(source_domain="flipkart.com", query="q", candidates=[], error="parser_empty: no cards")
    empty = SearchScrapeResult(source_domain="amazon.in", query="q", candidates=[])
    monkeypatch.setattr(flipkart, "search", lambda query: failed)
    monkeypatch.setattr(amazon, "search", lambda query: empty)

    rows = doctor.check_live_scrape()

    assert [status for status, _ in rows] == [doctor.FAIL, doctor.WARN]
    assert "search error" in rows[0][1] and "returned 0 products" in rows[1][1]


def test_the_scrape_check_passes_when_cards_parse(monkeypatch):
    from scrapers import amazon, flipkart
    from scrapers._base import CandidateResult, SearchScrapeResult

    card = CandidateResult(source_domain="amazon.in", url="https://www.amazon.in/dp/X", scraper_used="amazon",
                           structured_fields={"title": "Cable", "price": 399}, raw_page_text=None)
    found = SearchScrapeResult(source_domain="amazon.in", query="q", candidates=[card])
    monkeypatch.setattr(flipkart, "search", lambda query: found)
    monkeypatch.setattr(amazon, "search", lambda query: found)

    rows = doctor.check_live_scrape()

    assert [status for status, _ in rows] == [doctor.PASS, doctor.PASS]
    assert all("returned 1 products" in message for _, message in rows)


def test_failover_reports_when_nothing_answers_and_puts_the_key_back(monkeypatch):
    from llm import router

    captured = {}

    class NoAnswer:
        def __init__(self, endpoints, **kwargs):
            captured["keys"] = [e.api_key_env for e in endpoints]

        def complete(self, messages, max_tokens, purpose=""):
            raise RuntimeError("all down")

    endpoints = [
        router.Endpoint(provider="one", model="m", base_url="https://one.test/v1", api_key_env="ONE_KEY"),
        router.Endpoint(provider="two", model="m", base_url="https://two.test/v1", api_key_env="TWO_KEY"),
    ]
    monkeypatch.setattr(router, "load_endpoints", lambda: (endpoints, "failover"))
    monkeypatch.setattr(router, "Router", NoAnswer)
    monkeypatch.setenv("ONE_KEY", "real-one")

    [(status, message)] = doctor.check_live_failover()

    assert status == doctor.FAIL and "all down" in message
    assert captured["keys"] == ["ONE_KEY", "TWO_KEY"]
    assert __import__("os").environ["ONE_KEY"] == "real-one"


def test_failover_warns_when_the_broken_key_is_still_accepted(monkeypatch):
    from llm import router

    class StillReady:
        def __init__(self, endpoints, **kwargs):
            self.endpoints = endpoints

        def complete(self, messages, max_tokens, purpose=""):
            return '{"ok": true}'

        def status(self):
            return [{"endpoint": self.endpoints[0].name, "state": "ready", "calls": 0}]

    endpoints = [
        router.Endpoint(provider="one", model="m", base_url="https://one.test/v1", api_key_env="ONE_KEY"),
        router.Endpoint(provider="two", model="m", base_url="https://two.test/v1", api_key_env="TWO_KEY"),
    ]
    monkeypatch.setattr(router, "load_endpoints", lambda: (endpoints, "failover"))
    monkeypatch.setattr(router, "Router", StillReady)

    [(status, message)] = doctor.check_live_failover()

    assert status == doctor.WARN and "did not reject" in message


def test_run_checks_can_add_every_live_group(monkeypatch):
    seen = []
    monkeypatch.setattr(doctor, "OFFLINE_CHECKS", [])
    for name in ("check_live_llm", "check_live_engines", "check_live_scrape", "check_live_shops", "check_live_failover"):
        monkeypatch.setattr(doctor, name, lambda name=name: seen.append(name) or [(doctor.PASS, name)])

    results = doctor.run_checks(live_llm=True, live_engines=True, live_scrape=True, live_shops=True, live_failover=True)

    assert len(results) == 5 and [message for _, message in results] == seen


def test_clean_reports_folders_that_do_not_exist(monkeypatch, tmp_path):
    from core import pipeline, runs

    monkeypatch.setattr(pipeline, "TRACES_DIR", tmp_path / "no-traces")
    monkeypatch.setattr(runs, "runs_dir", lambda: tmp_path / "no-runs")
    monkeypatch.setattr(doctor, "DEBUG_HTML_DIR", tmp_path / "no-debug")

    rows = doctor.clean_old_files(30)

    assert [status for status, _ in rows] == [doctor.PASS, doctor.PASS, doctor.PASS]
    assert all("does not exist" in message for _, message in rows)


def test_clean_names_files_it_cannot_remove(monkeypatch, tmp_path):
    from core import pipeline, runs

    folder = tmp_path / "traces"
    folder.mkdir(exist_ok=True)
    (folder / "old.json").mkdir()
    monkeypatch.setattr(pipeline, "TRACES_DIR", folder)
    monkeypatch.setattr(runs, "runs_dir", lambda: tmp_path / "no-runs")
    monkeypatch.setattr(doctor, "DEBUG_HTML_DIR", tmp_path / "no-debug")

    rows = doctor.clean_old_files(0)

    assert rows[0][0] == doctor.FAIL and "could not remove 1 trace" in rows[0][1]


def test_main_clean_uses_the_requested_age_and_exits_on_failures(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(doctor, "clean_old_files", lambda days: seen.append(days) or [(doctor.PASS, "cleaned")])
    monkeypatch.setattr(__import__("sys"), "argv", ["doctor.py", "--clean", "--older-than", "3"])

    assert doctor.main() == 0
    assert seen == [3.0]
    assert "PASS  cleaned" in capsys.readouterr().out

    monkeypatch.setattr(doctor, "clean_old_files", lambda days: [(doctor.FAIL, "stuck")])
    monkeypatch.setattr(__import__("sys"), "argv", ["doctor.py", "--clean"])
    assert doctor.main() == 1
