"""Running the two marketplace searches at the same time."""
import threading
import time

import pytest

from core import events
from core.parallel import run_many, run_pair


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


# --- run_many: the same idea for a whole list of jobs --------------------------

def test_run_many_with_no_jobs_returns_empty_list():
    assert run_many([], workers=4) == []


def test_run_many_sequential_mode_runs_in_order():
    order = []
    fns = [lambda: order.append("a") or 1, lambda: order.append("b") or 2, lambda: order.append("c") or 3]
    assert run_many(fns, workers=1) == [1, 2, 3]
    assert order == ["a", "b", "c"]


def test_run_many_by_default_runs_one_at_a_time_in_this_thread():
    # workers=1 (the default) must behave like today's plain loop — the jobs
    # run in the caller's thread, not in pool threads.
    names = []
    run_many([lambda: names.append(threading.current_thread().name), lambda: names.append(threading.current_thread().name)])
    assert names == [threading.current_thread().name, threading.current_thread().name]


def test_run_many_parallel_mode_really_overlaps():
    gate = threading.Barrier(3, timeout=5)

    def job(x):
        gate.wait()  # only passes if all three jobs are running at once
        return x

    assert run_many([lambda: job("a"), lambda: job("b"), lambda: job("c")], workers=3) == ["a", "b", "c"]


def test_run_many_keeps_the_input_order():
    def slow(x):
        time.sleep(0.05 if x == "a" else 0.0)
        return x

    assert run_many([lambda x=x: slow(x) for x in ("a", "b", "c")], workers=3) == ["a", "b", "c"]


def test_run_many_never_runs_more_than_workers_at_once():
    lock = threading.Lock()
    running, peak = 0, 0

    def job(i):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.02)
        with lock:
            running -= 1
        return i

    assert run_many([lambda i=i: job(i) for i in range(4)], workers=2) == [0, 1, 2, 3]
    assert peak <= 2


def test_run_many_all_finish_before_the_first_error_is_raised():
    done = []

    def job(x):
        time.sleep(0.05 if x == "slow" else 0.0)
        done.append(x)
        if x == "boom":
            raise ValueError("bad listing")

    with pytest.raises(ValueError, match="bad listing"):
        run_many([lambda: job("slow"), lambda: job("boom")], workers=2)
    assert sorted(done) == ["boom", "slow"]


def test_run_many_raises_the_first_error_in_job_order():
    def boom1():
        raise ValueError("first problem")

    def boom2():
        raise TypeError("second problem")

    with pytest.raises(ValueError, match="first problem"):
        run_many([boom1, boom2], workers=2)


def test_run_many_events_from_all_jobs_reach_the_listener():
    seen = []

    def job(name):
        events.emit("search_started", marketplace=name)
        time.sleep(0.01)
        events.emit("search_finished", marketplace=name)

    with events.capture(seen.append):
        run_many([lambda: job("flipkart"), lambda: job("amazon"), lambda: job("croma")], workers=3)
    assert sorted({e["marketplace"] for e in seen}) == ["amazon", "croma", "flipkart"]
    assert len(seen) == 6
    assert [e["seq"] for e in seen] == sorted(e["seq"] for e in seen)
