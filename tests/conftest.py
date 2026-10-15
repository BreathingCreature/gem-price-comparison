import pytest

from core import pipeline
from scrapers import amazon, flipkart


@pytest.fixture(autouse=True)
def isolated_runtime_dirs(tmp_path, monkeypatch):
    """Point cache, traces and debug pages at a throwaway folder for every test."""
    for name in ("cache", "traces", "debug_html"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(pipeline, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(pipeline, "TRACES_DIR", tmp_path / "traces")
    monkeypatch.setattr(flipkart, "DEBUG_DIR", tmp_path / "debug_html")
    monkeypatch.setattr(amazon, "DEBUG_DIR", tmp_path / "debug_html")
