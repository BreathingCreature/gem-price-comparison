"""Every module in the project must import cleanly, and the Selenium fallback must be able to log."""
import importlib
import pkgutil
import sys

import api
import core
import discovery
import llm
import scrapers
from scrapers import _base


def test_every_module_imports():
    failures = []
    names = ["cli"]
    for package in (api, core, discovery, llm, scrapers):
        names.append(package.__name__)
        names += [info.name for info in pkgutil.walk_packages(package.__path__, prefix=package.__name__ + ".")]
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:  # collect all of them instead of stopping at the first
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, "\n".join(failures)


def test_driver_fallback_path_can_log(monkeypatch):
    """When undetected-chromedriver fails, build_stealth_driver logs and falls back to plain Selenium."""
    import selenium.webdriver as webdriver

    class ReachedSelenium(Exception):
        pass

    def fake_chrome(*args, **kwargs):
        raise ReachedSelenium

    monkeypatch.setitem(sys.modules, "undetected_chromedriver", None)  # makes the import fail
    monkeypatch.setattr(_base, "_USE_UC", None)
    monkeypatch.setattr(webdriver, "Chrome", fake_chrome)

    try:
        _base.build_stealth_driver()
    except ReachedSelenium:
        pass  # got past the log call and into plain Selenium
