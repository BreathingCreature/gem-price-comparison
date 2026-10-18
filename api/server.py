"""HTTP layer. Start it with:

    python -m uvicorn api.server:app --port 8000

Then open http://localhost:8000 (the frontend/ folder is served from there).
Set GEM_DEMO=1 to run a fake pipeline, handy for working on the frontend
without scraping anything or spending LLM calls. Set GEM_JOBS=2 to run two
comparisons at the same time (queued beyond that)

The contract (routes, event types) is written down in docs/api.md.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.jobs import JobManager
from core import pipeline
from core.config import BASE_DIR, logger

FRONTEND_DIR = BASE_DIR / "frontend"
SSE_HEARTBEAT_SECONDS = 15.0


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


def _looks_like_gem_url(url: str) -> bool:
    parts = urlparse(url.strip())
    host = (parts.hostname or "").lower()
    return parts.scheme in ("http", "https") and (
        host == "gem.gov.in" or host.endswith(".gem.gov.in")
    )


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
        if not demo and not _looks_like_gem_url(body.gem_url):
            raise HTTPException(status_code=422, detail="That does not look like a GeM product link (gem.gov.in).")
        job = manager.submit(body.gem_url.strip(), body.refresh)
        return {"run_id": job.id, "status": job.status}

    @app.get("/api/runs")
    def list_runs():
        return {"runs": manager.recent()}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        job = manager.get(run_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such run.")
        return {**job.summary(), "result": job.result, "error": job.error}

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
            return {"error": f"{type(exc).__name__}: {exc}", "endpoints": []}
        return {"strategy": router.strategy, "endpoints": router.status()}

    @app.get("/api/traces/{trace_id}")
    def get_trace(trace_id: str):
        if not re.fullmatch(r"[0-9a-f]{8,32}", trace_id):
            raise HTTPException(status_code=404, detail="No such trace.")
        path: Path = pipeline.TRACES_DIR / f"{trace_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="No such trace.")
        return json.loads(path.read_text(encoding="utf-8"))

    if FRONTEND_DIR.exists():
        # Mounted last so it never hides the /api routes above.
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    else:
        logger.warning("frontend/ folder not found, only the API is served.")

    return app


app = create_app()
