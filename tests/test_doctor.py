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

    monkeypatch.setattr(demo, "DEMO_DELAY", 0.5)
    status, _ = doctor.check_demo_run()
    assert status == doctor.PASS
    assert demo.DEMO_DELAY == 0.5


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
