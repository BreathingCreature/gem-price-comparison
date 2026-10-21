"""HTTP layer. Start it with:

    python -m uvicorn api.server:app --host 127.0.0.1 --port 8000

Then open http://127.0.0.1:8000 (the frontend/ folder is served from there).
The server binds to 127.0.0.1 by default: it is a personal tool, not a
public service. Set GEM_DEMO=1 to run the real pipeline with fake shops,
handy for working on the frontend without scraping anything or spending LLM
calls. Set GEM_JOBS=2 to run two comparisons at the same time (queued beyond
that).

The contract (routes, event types, result v2) is written down in docs/api.md.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.jobs import JobManager
from core import pipeline, runs as runs_store
from core.config import BASE_DIR, logger
from core.errors import redact
from core.gem_url import looks_like_gem_url

FRONTEND_DIR = BASE_DIR / "frontend"
SSE_HEARTBEAT_SECONDS = 15.0
TRACE_TEXT_LIMIT = 1000
_TRACE_TEXT_KEYS = ("raw_page_text", "page_text", "html", "raw_html")


class RunRequest(BaseModel):
    gem_url: str
    refresh: bool = False


def _real_runner(gem_url: str, refresh: bool, on_event: Callable[[dict], None]) -> dict:
    return pipeline.run_pipeline(gem_url, refresh=refresh, on_event=on_event)


def _warm_up() -> None:
    """E8: pay the first-run costs at server start instead of inside the
    user's first run — build the LLM router (reads llm_providers.json) and
    resolve the Chrome driver once (Selenium Manager does its download
    check on the very first call, 1-5 s). A broken environment is a warning
    here, not a surprise mid-run crash."""
    try:
        from llm.router import get_router

        get_router()
    except Exception as exc:
        logger.warning("LLM router warm-up failed: %s: %s", type(exc).__name__, exc)
    try:
        from scrapers._base import resolve_chromedriver

        resolve_chromedriver()
    except Exception as exc:
        logger.warning("Chrome driver warm-up failed: %s: %s", type(exc).__name__, exc)


def _trim_text(value, limit: int):
    """Recursively shorten saved page text in a trace. Everything else (the
    verdicts, prices, URLs) stays exactly as it was; only the raw HTML/text
    snapshots shrink, because 2 MB of a shop's page is not a debugging aid
    when the phone is reading the JSON."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _TRACE_TEXT_KEYS and isinstance(item, str) and len(item) > limit:
                out[key] = item[:limit] + f"... [{len(item) - limit} chars trimmed]"
            else:
                out[key] = _trim_text(item, limit)
        return out
    if isinstance(value, list):
        return [_trim_text(item, limit) for item in value]
    return value


def _recent_runs(manager: JobManager, limit: int = 20) -> list[dict]:
    """Live jobs first, then the saved records (so the list survives a
    restart). One entry per run id; the live job wins because it is newer."""
    entries: list[dict] = []
    seen: set[str] = set()
    for job in manager.recent(limit):
        entries.append(
            {
                "id": job["id"],
                "gem_url": job.get("gem_url"),
                "title": job.get("title"),
                "state": job.get("status"),
                "status": job.get("status"),
                "cheapest_price": job.get("cheapest_price"),
                "match_count": job.get("match_count"),
                "checked_at": None,
                "from_cache": False,
                "live": True,
            }
        )
        # A finished live job saves a record under its trace id, which is not
        # the job id. Dedupe by that id or the same run appears twice (once
        # live, once from cache/runs) as soon as it finishes.
        seen.add(job.get("run_id") or job["id"])
    for record in runs_store.list_runs(limit):
        run_id = str(record.get("run_id") or "")
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        entries.append(
            {
                "id": run_id,
                "gem_url": record.get("gem_url"),
                "title": record.get("title"),
                "state": record.get("state") or "done",
                "status": record.get("state") or "done",
                "cheapest_price": record.get("cheapest_price"),
                "match_count": record.get("match_count"),
                "checked_at": record.get("checked_at"),
                "from_cache": bool(record.get("from_cache")),
                "live": False,
            }
        )
    return entries[:limit]


def create_app(
    runner: Optional[Callable] = None,
    *,
    demo: Optional[bool] = None,
    warmup: Optional[bool] = None,
) -> FastAPI:
    if demo is None:
        demo = os.getenv("GEM_DEMO") == "1"
    if warmup is None:
        # Only the real app warms up; demo mode and injected runners
        # (the tests) must not touch the network or Chrome at startup.
        warmup = runner is None and not demo
    if runner is None:
        if demo:
            from api.demo import demo_runner

            runner = demo_runner
        else:
            runner = _real_runner

    # B9: one run at a time by default — two is the most Chrome and the
    # shops tolerate together, and only when asked for (GEM_JOBS=2).
    raw_jobs = os.getenv("GEM_JOBS", "1")
    try:
        max_parallel = max(1, int(raw_jobs.strip()))
    except ValueError:
        logger.warning("Invalid GEM_JOBS=%r, running one job at a time.", raw_jobs)
        max_parallel = 1
    manager = JobManager(runner, max_parallel=max_parallel)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if warmup:
            _warm_up()
        yield

    app = FastAPI(title="GeM Price Comparison API", lifespan=lifespan)
    app.state.manager = manager

    origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/health")
    def health():
        return {"ok": True, "demo": demo}

    @app.post("/api/runs", status_code=202)
    def start_run(body: RunRequest):
        gem_url = body.gem_url.strip()
        if not demo and not looks_like_gem_url(gem_url):
            raise HTTPException(status_code=422, detail="That does not look like a GeM product link (gem.gov.in).")
        job = manager.submit(gem_url, body.refresh)
        return {"run_id": job.id, "status": job.status}

    @app.get("/api/runs")
    def list_runs():
        return {"runs": _recent_runs(manager)}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        job = manager.get(run_id)
        if job is not None:
            summary = job.summary()
            result = job.result
            state = (result or {}).get("state") or job.status
            return {
                **summary,
                "id": job.id,
                "state": state,
                "result": result,
                "error": redact(job.error) if job.error else None,
                "live": True,
            }
        # Not in memory: a run saved before this process started. The record
        # holds the full v2 result (see pipeline._save_run_record).
        record = runs_store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="No such run.")
        result = record.get("result")
        return {
            "id": record.get("run_id"),
            "gem_url": record.get("gem_url"),
            "status": record.get("state") or "done",
            "state": record.get("state") or "done",
            "created": None,
            "started": None,
            "finished": None,
            "event_count": 0,
            "result": result,
            "error": None,
            "live": False,
        }

    @app.get("/api/runs/{run_id}/events")
    def stream_events(run_id: str, request: Request, after: int = 0):
        job = manager.get(run_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such run.")
        # A browser that reconnects sends Last-Event-ID; continue after it.
        last_id = request.headers.get("last-event-id")
        start = int(last_id) if last_id and last_id.isdigit() else after

        def stream():
            index = start
            while True:
                new, done = job.wait_for_events(index, SSE_HEARTBEAT_SECONDS)
                for event in new:
                    yield f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                index += len(new)
                if done:
                    yield f"event: done\ndata: {json.dumps({'status': job.status})}\n\n"
                    return
                if not new:
                    yield ": keep-alive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/llm/status")
    def llm_status():
        from llm.router import get_router

        try:
            router = get_router()
        except Exception as exc:  # a broken llm_providers.json should be visible, not a crash
            return {"error": redact(f"{type(exc).__name__}: {exc}"), "endpoints": []}
        return {"strategy": router.strategy, "endpoints": router.status()}

    @app.get("/api/traces/{trace_id}")
    def get_trace(trace_id: str):
        if not re.fullmatch(r"[0-9a-f]{8,32}", trace_id):
            raise HTTPException(status_code=404, detail="No such trace.")
        path: Path = pipeline.TRACES_DIR / f"{trace_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="No such trace.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # ValueError covers JSON/UTF-8 errors
            logger.warning("Trace %s could not be read: %s", trace_id, exc)
            raise HTTPException(status_code=500, detail="That trace file is damaged.") from exc
        if os.getenv("GEM_DEBUG") != "1":
            data = _trim_text(data, TRACE_TEXT_LIMIT)
        return data

    if FRONTEND_DIR.exists():
        # Mounted last so it never hides the /api routes above.
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    else:
        logger.warning("frontend/ folder not found, only the API is served.")

    return app


app = create_app()
