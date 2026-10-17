"""Run two jobs at the same time and wait for both.

Used to search Flipkart and Amazon together. Each search opens its own
browser, so they do not share anything.

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
