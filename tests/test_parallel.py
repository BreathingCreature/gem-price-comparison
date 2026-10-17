"""Running the two marketplace searches at the same time."""
import threading
import time

import pytest

from core import events
from core.parallel import run_pair


def test_sequential_mode_runs_in_order():
    order = []
    assert run_pair(lambda: order.append("a") or 1, lambda: order.append("b") or 2, parallel=False) == (1, 2)
    assert order == ["a", "b"]


def test_parallel_mode_really_overlaps():
    gate = threading.Barrier(2, timeout=5)

    def job(x):
        gate.wait()  # only passes if BOTH jobs are running at once
        return x

    assert run_pair(lambda: job("a"), lambda: job("b")) == ("a", "b")


def test_events_from_both_threads_reach_the_listener():
    seen = []

    def job(name):
        events.emit("search_started", marketplace=name)
        time.sleep(0.01)
        events.emit("search_finished", marketplace=name)

    with events.capture(seen.append):
        run_pair(lambda: job("flipkart"), lambda: job("amazon"))
    assert sorted(e["marketplace"] for e in seen if e["type"] == "search_started") == ["amazon", "flipkart"]
    assert sorted(e["marketplace"] for e in seen if e["type"] == "search_finished") == ["amazon", "flipkart"]
    assert [e["seq"] for e in seen] == sorted(e["seq"] for e in seen) and len({e["seq"] for e in seen}) == 4


def test_both_finish_before_an_error_is_raised():
    done = []

    def slow():
        time.sleep(0.1)
        done.append("slow")

    def boom():
        raise ValueError("flipkart blocked")

    with pytest.raises(ValueError, match="flipkart blocked"):
        run_pair(boom, slow)
    assert done == ["slow"]
