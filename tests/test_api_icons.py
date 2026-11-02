"""The favicon route is an outbound fetch, so its safety rules get fakes.

Every network call is replaced here. These tests prove the route refuses
private, oversized, non-image and cross-domain answers, and that a cached icon
is served without another request.
"""
import socket
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api import server
from api.jobs import Job
from api.server import create_app


class FakeResponse:
    def __init__(self, *, status=200, body=b"", content_type="image/png", headers=None, chunks=None):
        self.status_code = status
        self.headers = dict(headers or {})
        if content_type is not None:
            self.headers.setdefault("Content-Type", content_type)
        self._body = body
        self._chunks = list(chunks) if chunks is not None else None

    def iter_content(self, size):
        if self._chunks is not None:
            yield from self._chunks
            return
        for start in range(0, len(self._body), size):
            yield self._body[start:start + size]

    def close(self):
        pass


def _public(monkeypatch):
    monkeypatch.setattr(server, "_host_is_public", lambda host: True)


def test_a_small_image_is_accepted(monkeypatch):
    _public(monkeypatch)
    monkeypatch.setattr(server.requests, "get", lambda *a, **k: FakeResponse(body=b"\x89PNG"))
    data, content_type = server._safe_icon_get("https://shop.example/favicon.ico", "shop.example")
    assert data == b"\x89PNG"
    assert content_type == "image/png"


def test_a_response_over_the_size_limit_is_refused(monkeypatch):
    _public(monkeypatch)
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *a, **k: FakeResponse(body=b"x" * 100001, headers={"Content-Length": "100001"}),
    )
    assert server._safe_icon_get("https://shop.example/favicon.ico", "shop.example") is None


def test_a_stream_without_content_length_stops_at_the_limit(monkeypatch):
    _public(monkeypatch)
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *a, **k: FakeResponse(chunks=[b"x" * 60000, b"x" * 60000]),
    )
    assert server._safe_icon_get("https://shop.example/favicon.ico", "shop.example") is None


def test_a_non_image_content_type_is_refused(monkeypatch):
    _public(monkeypatch)
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *a, **k: FakeResponse(body=b"<html>", content_type="text/html"),
    )
    assert server._safe_icon_get("https://shop.example/favicon.ico", "shop.example") is None


def test_a_redirect_to_another_domain_is_refused_before_the_second_request(monkeypatch):
    _public(monkeypatch)
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return FakeResponse(status=302, headers={"Location": "https://evil.example/icon.ico"})

    monkeypatch.setattr(server.requests, "get", fake_get)
    assert server._safe_icon_get("https://shop.example/favicon.ico", "shop.example") is None
    assert calls == ["https://shop.example/favicon.ico"]


def test_a_private_address_is_not_public(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
    )
    assert server._host_is_public("shop.example") is False


def test_a_cached_icon_is_not_fetched_again(tmp_path, monkeypatch):
    calls = []

    def fetch(url, domain, **kwargs):
        calls.append(url)
        return b"icon-bytes", "image/x-icon"

    monkeypatch.setattr(server, "_safe_icon_get", fetch)
    first = server._fetch_site_icon("shop.example", tmp_path)
    second = server._fetch_site_icon("shop.example", tmp_path)
    assert first == second == (b"icon-bytes", "image/x-icon")
    assert calls == ["https://shop.example/favicon.ico"]
    assert (tmp_path / "shop.example.bin").read_bytes() == b"icon-bytes"


def test_the_route_serves_an_icon_for_a_domain_in_a_live_run(tmp_path, monkeypatch):
    app = create_app(runner=lambda *a: {})
    job = Job("https://mkp.gem.gov.in/computer-mouse/one/p-1-2-cat.html", False)
    job.result = {"shops": {"example-shop.in": {"status": "found"}}, "matches": []}
    app.state.manager._jobs[job.id] = job
    monkeypatch.setattr(server, "_fetch_site_icon", lambda domain, cache_dir: (b"png", "image/png"))
    response = TestClient(app).get("/api/icon/example-shop.in")
    assert response.status_code == 200
    assert response.content == b"png"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "max-age=86400"


def test_an_unknown_domain_gets_no_icon_and_no_fetch(monkeypatch):
    app = create_app(runner=lambda *a: {})

    def must_not_fetch(*args, **kwargs):
        raise AssertionError("the route fetched a domain that no run contains")

    monkeypatch.setattr(server, "_fetch_site_icon", must_not_fetch)
    response = TestClient(app).get("/api/icon/unknown-shop.in")
    assert response.status_code == 204


def test_the_route_refuses_a_bad_domain_spelling():
    app = create_app(runner=lambda *a: {})
    response = TestClient(app).get("/api/icon/UPPER")
    assert response.status_code == 404


def test_a_saved_run_can_authorise_a_domain(monkeypatch):
    record = {
        "run_id": "abc",
        "gem_url": "https://mkp.gem.gov.in/a/b/p-1-2-cat.html",
        "result": {"shops": {"saved-shop.in": {"status": "not_found"}}},
    }
    monkeypatch.setattr(server.runs_store, "list_runs", lambda limit=20: [record])
    manager = SimpleNamespace(_jobs={})
    assert server._domain_in_saved_runs("saved-shop.in", manager) is True


def test_a_title_that_contains_a_domain_does_not_authorise_it(monkeypatch):
    record = {
        "run_id": "abc",
        "gem_url": "https://mkp.gem.gov.in/a/b/p-1-2-cat.html",
        "title": "A listing that mentions saved-shop.in in its title",
        "result": {"shops": {"other.example": {"status": "not_found"}}},
    }
    monkeypatch.setattr(server.runs_store, "list_runs", lambda limit=20: [record])
    manager = SimpleNamespace(_jobs={})
    assert server._domain_in_saved_runs("saved-shop.in", manager) is False


def test_demo_mode_returns_no_icon_instead_of_making_a_network_call(monkeypatch):
    app = create_app(demo=True)
    job = Job("https://mkp.gem.gov.in/a/b/p-1-2-cat.html", False)
    job.result = {"shops": {"example-shop.in": {"status": "found"}}, "matches": []}
    app.state.manager._jobs[job.id] = job
    monkeypatch.setattr(
        server,
        "_fetch_site_icon",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("demo made a network call")),
    )
    response = TestClient(app).get("/api/icon/example-shop.in")
    assert response.status_code == 204


def test_a_missing_provider_icon_answers_204(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "FRONTEND_DIR", tmp_path)
    client = TestClient(create_app(runner=lambda *a: {}))
    assert client.get("/icons/nvidia.svg").status_code == 204
    assert client.head("/icons/nvidia.svg").status_code == 204


def test_a_provider_icon_file_is_served(tmp_path, monkeypatch):
    icons = tmp_path / "icons"
    icons.mkdir()
    (icons / "nvidia.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
    monkeypatch.setattr(server, "FRONTEND_DIR", tmp_path)
    response = TestClient(create_app(runner=lambda *a: {})).get("/icons/nvidia.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["cache-control"] == "max-age=86400"
    assert b"<svg" in response.content


def test_the_provider_icon_route_refuses_a_name_that_is_not_a_plain_svg():
    client = TestClient(create_app(runner=lambda *a: {}))
    assert client.get("/icons/NVIDIA.svg").status_code == 404
    assert client.get("/icons/nvidia.png").status_code == 404
    assert client.get("/icons/%2e%2e%2fsecret.svg").status_code == 404
