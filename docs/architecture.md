# Architecture

```
cli.py ─┐
        ├─> core/pipeline.py ─> scrapers/  (GeM page, Flipkart, Amazon, any other shop)
api/ ───┘        │             ─> discovery/ (find other shops)
 ▲               │             ─> llm/client.py ─> llm/router.py ─> provider A / B / C
 │               └─> core/events.py  (progress events)
frontend/  <── server-sent events ── api/server.py
```

- `core/pipeline.py` is the one entry point. The command line and the web
  API both call `run_pipeline`. Nothing else decides how a comparison runs.
- `core/events.py` lets the pipeline narrate what it is doing without knowing
  who listens. No listener, no cost. The API forwards the events to the browser,
  and the same events are saved in each trace.
- `llm/router.py` hides how many LLM providers exist. See `llm-providers.md`.
- `api/` is a thin HTTP layer: start a run, stream its events, return the
  result. See `api.md` for the contract.
- `frontend/` is plain HTML, CSS and JavaScript with no build step. Because it
  only uses the routes in `api.md`, it can be replaced by a React or Vue app
  later without touching the backend. Set `window.GEM_API_BASE` to point it at
  a server on another address.

The package layout is deliberately flat. It is small enough that a `src/`
folder would add nothing.
