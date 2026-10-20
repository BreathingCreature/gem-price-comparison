"""The result cache key must change whenever the answer could change."""
from core import pipeline

URL = "https://mkp.gem.gov.in/test/cache-key/p-1-1-cat.html"


def _path(monkeypatch, **overrides):
    monkeypatch.setattr(pipeline, "_model_names", lambda: [])
    for name, value in overrides.items():
        monkeypatch.setattr(pipeline, name, value)
    return pipeline._cache_path(URL)


def test_the_same_settings_give_the_same_path(monkeypatch):
    assert _path(monkeypatch) == _path(monkeypatch)


def test_confidence_threshold_changes_the_key(monkeypatch):
    before = _path(monkeypatch)
    assert _path(monkeypatch, MATCH_CONFIDENCE_THRESHOLD=0.9) != before


def test_price_band_changes_the_key(monkeypatch):
    before = _path(monkeypatch)
    assert _path(monkeypatch, PRICE_SANITY_LOW_RATIO=0.2) != before
    assert _path(monkeypatch, PRICE_SANITY_HIGH_RATIO=4.0) != before


def test_prefilter_and_retry_settings_change_the_key(monkeypatch):
    before = _path(monkeypatch)
    assert _path(monkeypatch, PREFILTER_TOP=5) != before
    assert _path(monkeypatch, RETRY_ENABLED=False) != before


def test_the_model_list_changes_the_key(monkeypatch):
    monkeypatch.setattr(pipeline, "_model_names", lambda: ["a/one"])
    first = pipeline._cache_path(URL)
    monkeypatch.setattr(pipeline, "_model_names", lambda: ["b/two"])
    assert pipeline._cache_path(URL) != first


def test_the_result_version_changes_the_key(monkeypatch):
    before = _path(monkeypatch)
    assert _path(monkeypatch, RESULT_VERSION=99) != before
