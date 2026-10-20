"""Progress events for a pipeline run.

The pipeline calls emit("some_type", key=value, ...) whenever something
worth showing happens. If nobody is listening, emit does nothing. If a
listener is attached (the web API, a test, the CLI), it receives one small
dict per event:

    {"seq": 7, "t": 12.481, "type": "search_finished", ...your fields...}

seq is the order number, t is seconds since the run started.

A listener is attached with capture(callback). It lives in a contextvar,
so two runs in two threads never see each other's events.
"""
from __future__ import annotations

import contextvars
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

from core.errors import redact_obj

_current = contextvars.ContextVar("gem_event_sink", default=None)


class EventSink:
    def __init__(self, callback: Optional[Callable[[dict], None]] = None):
        self.callback = callback
        self.started = time.monotonic()
        self.events: list[dict] = []
        self._seq = 0
        self._lock = threading.Lock()

    def emit(self, type_: str, **data) -> dict:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "t": round(time.monotonic() - self.started, 3), "type": type_, **data}
            event = redact_obj(event)
            self.events.append(event)
        if self.callback is not None:
            try:
                self.callback(event)
            except Exception:
                # A broken listener must never break a pipeline run.
                pass
        return event


@contextmanager
def capture(callback: Optional[Callable[[dict], None]] = None):
    """Attach a listener for everything emitted inside the with-block."""
    sink = EventSink(callback)
    token = _current.set(sink)
    try:
        yield sink
    finally:
        _current.reset(token)


def emit(type_: str, **data) -> Optional[dict]:
    sink = _current.get()
    if sink is None:
        return None
    return sink.emit(type_, **data)


def snapshot() -> list:
    """Every event emitted so far in the current capture (empty if none)."""
    sink = _current.get()
    return list(sink.events) if sink is not None else []
