"""Task 1.3: a test that touches the network must fail loudly."""
import socket

import pytest


def test_create_connection_to_a_real_host_is_blocked():
    with pytest.raises(RuntimeError, match="Network access in a test is not allowed"):
        socket.create_connection(("example.com", 80), timeout=0.1)


def test_hostname_lookup_is_blocked():
    with pytest.raises(RuntimeError, match="Network access in a test is not allowed"):
        socket.getaddrinfo("example.com", 80)


def test_local_addresses_are_still_allowed():
    assert socket.getaddrinfo("127.0.0.1", 80)
