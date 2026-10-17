"""
Tests for frontend/app.js (F1).

There is no JS test setup in this repo, so these are simple string checks
over the file's SOURCE. Browser behaviour is NOT run here — see the final
report note. The guarantees tested:

- a safeHref() guard exists which only lets http:// or https:// URLs through
- no h("a" anchor call builds its href from a raw candidate_url anymore —
  scraped URLs are untrusted, so they must pass through safeHref() first
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

APP_JS = Path(__file__).resolve().parent.parent / "frontend" / "app.js"
SOURCE = APP_JS.read_text(encoding="utf-8")


def test_app_js_defines_safehref_guard():
    assert "function safeHref(" in SOURCE
    # the guard whitelists only real web schemes and falls back to null
    body = SOURCE.split("function safeHref(", 1)[1]
    assert "https" in body
    assert "return null" in body


def test_app_js_anchors_never_use_raw_candidate_url():
    # every h("a" anchor must take its href from safeHref(...) — a raw
    # candidate_url could be anything a scraped page produced
    lines = [ln.strip() for ln in SOURCE.splitlines() if 'h("a"' in ln]
    assert lines, 'expected at least one h("a"...) call in app.js'
    bad = []
    for line in lines:
        if "candidate_url" in line and "safeHref" not in line:
            bad.append(line)
    assert not bad, f"anchor(s) using raw candidate_url: {bad}"
    assert "safeHref(m.candidate_url)" in SOURCE


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
