"""The frontend is plain files served by the API. These checks catch a broken page early."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from api.server import FRONTEND_DIR, create_app


def _client():
    return TestClient(create_app(runner=lambda *a: {}, demo=False))


def test_the_page_is_served_from_the_root():
    response = _client().get("/")
    assert response.status_code == 200
    assert "GeM Price Comparison" in response.text


def test_the_page_loads_its_script_and_style():
    client = _client()
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200


def test_every_element_the_script_looks_up_exists_in_the_page():
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    script = (FRONTEND_DIR / "app.js").read_text(encoding="utf-8")
    ids_in_page = set(re.findall(r'id="([^"]+)"', html))
    ids_used = set(re.findall(r'\$\("([a-z-]+)"\)', script))
    # ids made on the fly by the script (search-flipkart and so on) are not in the page
    missing = {i for i in ids_used if i not in ids_in_page and not i.startswith("search-")}
    assert not missing, f"app.js uses ids that index.html does not have: {sorted(missing)}"


def test_the_api_routes_are_not_hidden_by_the_static_files():
    assert _client().get("/api/health").json()["ok"] is True


def test_the_script_only_calls_routes_that_exist():
    script = (FRONTEND_DIR / "app.js").read_text(encoding="utf-8")
    called = set(re.findall(r'API \+ "(/api/[a-z/]+)', script))
    assert called == {"/api/health", "/api/runs", "/api/runs/", "/api/llm/status", "/api/traces/"}
