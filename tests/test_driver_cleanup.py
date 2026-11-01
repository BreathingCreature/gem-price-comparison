"""Leftover browsers and drivers are closed, and only tracked PIDs are killed."""
import pytest

from scrapers import _base as base


class FakeProcess:
    def __init__(self, pid=4242, running=True):
        self.pid = pid
        self.running = running

    def poll(self):
        return None if self.running else 0


class FakeService:
    def __init__(self, process):
        self.process = process


class FakeDriver:
    def __init__(self, pid=4242, quit_error=None):
        self.service = FakeService(FakeProcess(pid))
        self.quit_error = quit_error
        self.quit_calls = 0

    def quit(self):
        self.quit_calls += 1
        if self.quit_error is not None:
            raise self.quit_error
        self.service.process.running = False


class FakeBrowser:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr(base, "_LIVE_DRIVERS", {})
    monkeypatch.setattr(base, "_LIVE_BROWSERS", {})
    monkeypatch.setattr(base, "_PREVIOUS_HANDLERS", {})
    monkeypatch.setattr(base, "_HOOKS_INSTALLED", False)
    monkeypatch.setattr(base, "_SIGNALS_INSTALLED", False)
    yield
    base._LIVE_DRIVERS.clear()
    base._LIVE_BROWSERS.clear()


def test_cleanup_quits_a_registered_driver_once(monkeypatch):
    killed = []
    monkeypatch.setattr(base, "_kill_pid_tree", killed.append)
    driver = FakeDriver()

    base.register_driver(driver)
    base.cleanup_drivers()
    base.cleanup_drivers()

    assert driver.quit_calls == 1
    assert base._LIVE_DRIVERS == {}
    assert killed == []


def test_cleanup_kills_only_the_pid_it_tracked(monkeypatch):
    killed = []
    monkeypatch.setattr(base, "_kill_pid_tree", killed.append)
    stuck = FakeDriver(pid=1111, quit_error=RuntimeError("still open"))
    other = FakeDriver(pid=2222, quit_error=RuntimeError("not ours"))

    base.register_driver(stuck)
    base.cleanup_drivers()

    assert stuck.quit_calls == 1
    assert killed == [1111]
    assert other.quit_calls == 0


def test_forget_driver_removes_it_from_cleanup(monkeypatch):
    monkeypatch.setattr(base, "_kill_pid_tree", lambda pid: None)
    driver = FakeDriver()

    base.register_driver(driver)
    base.forget_driver(driver)
    base.cleanup_drivers()

    assert driver.quit_calls == 0


def test_cleanup_closes_a_registered_browser():
    browser = FakeBrowser()
    base.register_browser(browser)

    base.cleanup_drivers()

    assert browser.close_calls == 1
    assert base._LIVE_BROWSERS == {}


def test_hooks_are_registered_once(monkeypatch):
    calls = []
    monkeypatch.setattr(base, "_HOOKS_INSTALLED", False)
    monkeypatch.setattr(base, "_SIGNALS_INSTALLED", False)
    monkeypatch.setattr(base.atexit, "register", lambda function: calls.append(("atexit", function)))
    monkeypatch.setattr(base.signal, "getsignal", lambda signum: None)
    monkeypatch.setattr(base.signal, "signal", lambda signum, handler: calls.append(("signal", signum)))

    base.install_cleanup_hooks()
    base.install_cleanup_hooks()

    assert [kind for kind, _ in calls].count("atexit") == 1
    assert [kind for kind, _ in calls].count("signal") == 2


def test_signals_are_retried_when_the_first_registration_was_not_on_the_main_thread(monkeypatch):
    calls = []
    on_main_thread = {"value": False}

    def refuse_off_main_thread(signum, handler):
        if not on_main_thread["value"]:
            raise ValueError("signal only works in main thread")
        calls.append(signum)

    monkeypatch.setattr(base.atexit, "register", lambda function: None)
    monkeypatch.setattr(base.signal, "getsignal", lambda signum: None)
    monkeypatch.setattr(base.signal, "signal", refuse_off_main_thread)

    base.install_cleanup_hooks()
    assert base._SIGNALS_INSTALLED is False
    assert calls == []

    on_main_thread["value"] = True
    base.install_cleanup_hooks()

    assert base._SIGNALS_INSTALLED is True
    assert calls == [base.signal.SIGINT, base.signal.SIGTERM]


def test_the_signal_handler_cleans_up_then_chains(monkeypatch):
    cleaned = []
    chained = []
    monkeypatch.setattr(base, "cleanup_drivers", lambda: cleaned.append(True))
    monkeypatch.setattr(base, "_PREVIOUS_HANDLERS", {base.signal.SIGINT: lambda signum, frame: chained.append(signum)})

    base._handle_cleanup_signal(base.signal.SIGINT, None)

    assert cleaned == [True]
    assert chained == [base.signal.SIGINT]


def test_flipkart_browser_registers_and_forgets_its_driver(monkeypatch):
    from scrapers import flipkart

    driver = FakeDriver()
    browser = flipkart.FlipkartBrowser(build_driver=lambda: driver)
    browser.start()
    assert browser._ready.wait(2)
    assert id(driver) in base._LIVE_DRIVERS

    browser.close()

    assert driver.quit_calls == 1
    assert id(driver) not in base._LIVE_DRIVERS
    assert id(browser) not in base._LIVE_BROWSERS


def test_register_ignores_none():
    base.register_driver(None)
    base.register_browser(None)
    assert base._LIVE_DRIVERS == {} and base._LIVE_BROWSERS == {}


def test_process_running_is_optimistic_without_a_poll():
    class NoPoll:
        pid = 1

    driver = type("Driver", (), {"service": type("Service", (), {"process": NoPoll()})()})()
    assert base._process_running(driver) is True
    assert base._driver_pid(driver) == 1


def test_process_running_treats_a_broken_poll_as_still_running():
    class BrokenPoll:
        pid = 1

        def poll(self):
            raise OSError("handle closed")

    driver = type("Driver", (), {"service": type("Service", (), {"process": BrokenPoll()})()})()
    assert base._process_running(driver) is True
    assert base._driver_pid(type("Driver", (), {"service": None})()) is None


def test_kill_pid_tree_on_windows_uses_taskkill_for_that_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(base.os, "name", "nt")
    monkeypatch.setattr(base.subprocess, "run", lambda args, **kwargs: calls.append(args))

    base._kill_pid_tree(4321)

    assert calls == [["taskkill", "/PID", "4321", "/T", "/F"]]


def test_kill_pid_tree_on_posix_signals_only_that_pid(monkeypatch):
    killed = []
    monkeypatch.setattr(base.os, "name", "posix")
    monkeypatch.setattr(base.os, "kill", lambda pid, signum: killed.append((pid, signum)))

    base._kill_pid_tree(9876)

    assert killed == [(9876, base.signal.SIGTERM)]


def test_cleanup_survives_a_browser_that_fails_to_close(monkeypatch, caplog):
    class BadBrowser:
        def close(self):
            raise RuntimeError("close failed")

    driver = FakeDriver()
    monkeypatch.setattr(base, "_kill_pid_tree", lambda pid: None)
    base.register_browser(BadBrowser())
    base.register_driver(driver)

    base.cleanup_drivers()

    assert driver.quit_calls == 1
    assert "Could not close a leftover browser." in caplog.text
    assert base._LIVE_BROWSERS == {} and base._LIVE_DRIVERS == {}


def test_the_signal_handler_exits_when_there_is_no_previous_handler(monkeypatch):
    monkeypatch.setattr(base, "cleanup_drivers", lambda: None)
    monkeypatch.setattr(base, "_PREVIOUS_HANDLERS", {})

    with pytest.raises(SystemExit) as exit_info:
        base._handle_cleanup_signal(base.signal.SIGTERM, None)
    assert exit_info.value.code == 128 + base.signal.SIGTERM

    with pytest.raises(KeyboardInterrupt):
        base._handle_cleanup_signal(base.signal.SIGINT, None)
