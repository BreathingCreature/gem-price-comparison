"""Every module in the project must import cleanly."""
import importlib
import pkgutil

import api
import core
import discovery
import llm
import scrapers


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
