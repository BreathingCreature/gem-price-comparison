"""tools/save_page.py: saves a real page as a fixture (never runs for real here)."""
from pathlib import Path

from tools import save_page


def test_default_name_is_a_clean_slug():
    assert save_page.default_name("flipkart", "USB C Cable / 1 m") == "flipkart-usb-c-cable-1-m"


def test_save_page_writes_the_html(monkeypatch, tmp_path):
    monkeypatch.setattr(save_page, "FIXTURES_DIR", tmp_path)
    path = save_page.save_page("flipkart", "usb c cable", fetcher=lambda shop, target: "<html>cards</html>")
    assert path == tmp_path / "flipkart-usb-c-cable.html"
    assert path.read_text(encoding="utf-8") == "<html>cards</html>"


def test_an_explicit_name_is_used(tmp_path):
    path = save_page.save_page("amazon", "https://example.test/x", name="amazon-case-1",
                               fetcher=lambda shop, target: "<html></html>", folder=tmp_path)
    assert path.name == "amazon-case-1.html"


def test_nothing_is_saved_when_the_fetch_returns_nothing(tmp_path):
    try:
        save_page.save_page("gem", "https://example.test/x", fetcher=lambda shop, target: None, folder=tmp_path)
    except RuntimeError as exc:
        assert "nothing saved" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
    assert list(tmp_path.glob("*.html")) == []


def test_a_query_with_a_shop_that_needs_a_url_is_an_error(monkeypatch):
    monkeypatch.setattr(save_page, "fetch_html", save_page.fetch_html)
    try:
        save_page.fetch_html("gem", "usb c cable")
    except ValueError as exc:
        assert "needs a full URL" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_main_reports_a_failure_without_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(save_page, "save_page", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")))
    assert save_page.main(["flipkart", "usb c cable"]) == 1
    assert "could not save the page" in capsys.readouterr().out


def test_the_fixture_folder_lives_under_tests():
    assert Path(save_page.FIXTURES_DIR).parts[-3:] == ("tests", "fixtures", "pages")
