"""Runs pipeline jobs in the background and keeps their events for the browser.

A Job is one comparison run. It collects the progress events the pipeline
emits, so a browser can connect at any moment (even after the run started or
finished) and replay everything from the beginning.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Optional

from core.config import logger

FINAL_STATES = ("done", "failed")


class Job:
    def __init__(self, gem_url: str, refresh: bool):
        self.id = uuid.uuid4().hex[:12]
        self.gem_url = gem_url
        self.refresh = refresh
        self.status = "queued"  # queued -> running -> done | failed
        self.events: list[dict] = []
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self._cond = threading.Condition()

    def add_event(self, event: dict) -> None:
        with self._cond:
            self.events.append(event)
            self._cond.notify_all()

    def finish(self, *, result: Optional[dict] = None, error: Optional[str] = None) -> None:
        with self._cond:
            self.finished = time.time()
            if error is not None:
                self.error = error
                self.status = "failed"
                self.events.append(
                    {
                        "seq": len(self.events) + 1,
                        "t": round(self.finished - (self.started or self.finished), 3),
                        "type": "run_failed",
                        "error": error,
                    }
                )
            else:
                self.result = result
                self.status = "done"
            self._cond.notify_all()

    def wait_for_events(self, start: int, timeout: float) -> tuple[list[dict], bool]:
        """Events after position `start`, waiting up to `timeout` seconds if
        there are none yet. Second value is True once the job is over (and
        then the returned list already holds every remaining event)."""
        with self._cond:
            if len(self.events) <= start and self.status not in FINAL_STATES:
                self._cond.wait(timeout)
            return list(self.events[start:]), self.status in FINAL_STATES

    def summary(self) -> dict:
        return {
            "id": self.id,
            "gem_url": self.gem_url,
            "refresh": self.refresh,
            "status": self.status,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "event_count": len(self.events),
        }


class JobManager:
    """runner(gem_url, refresh, on_event) -> result dict. Only max_parallel
    jobs run at once; the rest wait in 'queued' (the scrapers share one
    browser setup and the LLM has rate limits, so one at a time is safest)."""

    def __init__(self, runner: Callable[[str, bool, Callable[[dict], None]], dict], *, max_parallel: int = 1, keep: int = 50):
        self._runner = runner
        self._slots = threading.Semaphore(max_parallel)
        self._keep = keep
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def submit(self, gem_url: str, refresh: bool = False) -> Job:
        job = Job(gem_url, refresh)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._forget_old_jobs()
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"job-{job.id}").start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            ids = self._order[-limit:][::-1]
            return [self._jobs[i].summary() for i in ids]

    def _forget_old_jobs(self) -> None:
        while len(self._order) > self._keep:
            oldest = self._jobs.get(self._order[0])
            if oldest is not None and oldest.status not in FINAL_STATES:
                break
            self._jobs.pop(self._order.pop(0), None)

    def _run(self, job: Job) -> None:
        with self._slots:
            job.status = "running"
            job.started = time.time()
            try:
                result = self._runner(job.gem_url, job.refresh, job.add_event)
            except Exception as exc:
                logger.exception("Run %s failed", job.id)
                job.finish(error=f"{type(exc).__name__}: {exc}")
            else:
                job.finish(result=result)
