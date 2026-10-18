"""Run jobs at the same time and wait for all of them.

run_pair runs two jobs (the Flipkart and Amazon searches); run_many runs a
whole list with a worker limit (used to verify listings and to retry).
Each job opens its own browser or HTTP session, so they do not share
anything.

One detail matters: progress events are tied to the run by a context
variable, and a new thread does not get it automatically. Each job is
started inside a copy of the caller's context, so events from both searches
still reach the live page.
"""
from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor, wait


def run_pair(first, second, parallel: bool = True):
    """Call first() and second() (no arguments each). Returns (r1, r2).

    With parallel=False they run one after the other, first then second.
    If either one raises, both are still allowed to finish, then the
    first error is raised."""
    if not parallel:
        return first(), second()
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="gem-search") as pool:
        f1 = pool.submit(contextvars.copy_context().run, first)
        f2 = pool.submit(contextvars.copy_context().run, second)
        wait([f1, f2])
    for f in (f1, f2):
        if f.exception() is not None:
            raise f.exception()
    return f1.result(), f2.result()


def run_many(jobs, workers: int = 1):
    """Call every job (no arguments each) with at most `workers` at a time.
    Returns the results in the same order as the jobs.

    workers=1 (the default) runs them one after the other in this thread —
    exactly today's behaviour. If any job raises, the rest are still
    allowed to finish, then the first error (in job order) is raised."""
    if workers <= 1 or len(jobs) <= 1:
        return [job() for job in jobs]
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs)), thread_name_prefix="gem-job") as pool:
        futures = [pool.submit(contextvars.copy_context().run, job) for job in jobs]
        wait(futures)
    for f in futures:
        if f.exception() is not None:
            raise f.exception()
    return [f.result() for f in futures]
