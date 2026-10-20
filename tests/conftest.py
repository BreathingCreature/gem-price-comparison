import socket

import pytest

from core import config, pipeline
from scrapers import amazon, flipkart


@pytest.fixture(autouse=True)
def isolated_runtime_dirs(tmp_path, monkeypatch):
    """Point cache, traces and debug pages at a throwaway folder for every test."""
    for name in ("cache", "traces", "debug_html"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(pipeline, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(pipeline, "TRACES_DIR", tmp_path / "traces")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(flipkart, "DEBUG_DIR", tmp_path / "debug_html")
    monkeypatch.setattr(amazon, "DEBUG_DIR", tmp_path / "debug_html")


_LOCAL_NAMES = {"localhost", "::1", ""}


def _is_local(host) -> bool:
    if host is None or not isinstance(host, str):
        return True
    host = host.split("%")[0]
    return host in _LOCAL_NAMES or host == "127.0.0.1" or host.startswith("127.")


def _address_host(address):
    if isinstance(address, tuple) and address:
        return address[0]
    return None


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Make any real network call in a test fail loudly.

    Task 1.3: tests must use fakes, never a live shop or LLM. Local addresses
    stay usable because the test client and helper servers may use them.
    """
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def guarded_create_connection(address, *args, **kwargs):
        if not _is_local(_address_host(address)):
            raise RuntimeError("Network access in a test is not allowed")
        return real_create_connection(address, *args, **kwargs)

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if not _is_local(host):
            raise RuntimeError("Network access in a test is not allowed")
        return real_getaddrinfo(host, port, *args, **kwargs)

    def guarded_connect(self, address):
        if not _is_local(_address_host(address)):
            raise RuntimeError("Network access in a test is not allowed")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
