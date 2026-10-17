# API and event contract

The frontend only knows this page. Anything not described here is an internal
detail and may change.

Start the server: `python -m uvicorn api.server:app --port 8000`, then open
http://localhost:8000. Set `GEM_DEMO=1` first to run a fake pipeline (no
scraping, no LLM calls), which is the easy way to work on the frontend.

## Routes

| Method and path | What it does |
|---|---|
| `GET /api/health` | `{"ok": true, "demo": false}` |
| `POST /api/runs` | Body `{"gem_url": "...", "refresh": false}`. Starts a run, answers `202 {"run_id", "status"}`. Answers 422 if the link is not a gem.gov.in link (demo mode accepts any link). |
| `GET /api/runs` | The 20 most recent runs, newest first. |
| `GET /api/runs/{id}` | Status (`queued`, `running`, `done`, `failed`), timestamps, `result` once done, `error` if failed. |
| `GET /api/runs/{id}/events` | Server-sent events. Replays every event from the start, then follows the live run. `?after=N` (or the `Last-Event-ID` header) continues after event number N. Ends with `event: done` and `{"status": "done"}` or `"failed"`. |
| `GET /api/llm/status` | The LLM router's view: strategy and, per endpoint, state (`ready`, `cooling`, `disabled`), calls, errors, average seconds, last error. |
| `GET /api/traces/{trace_id}` | The saved trace JSON of a finished run (it includes every event). |

Only one run executes at a time. Others wait in `queued`.

## Events

Every event is one JSON object: `seq` (1, 2, 3 ...), `t` (seconds since the
run started), `type`, plus the fields below.

| type | fields | meaning |
|---|---|---|
| `run_started` | gem_url, refresh | the run began |
| `cache_hit` | gem_url | a saved result is being reused (run ends right after) |
| `stage` | name, label | a new step began (the previous one is over) |
| `gem_product` | title, price, price_type, brand, model, category, specs | the GeM page was read |
| `normalized` | canonical_name, brand, model, queries, key_identifiers | the LLM's understanding of the product |
| `search_started` | marketplace, query | a Flipkart or Amazon search began |
| `search_finished` | marketplace, query, count, blocked, error, candidates[] | results; each candidate has title, price, url, domain, error |
| `discovery_finished` | queries, urls | other shop pages worth opening |
| `scrape_started` / `scrape_finished` | url / title, price, url, domain, error | one of those pages was opened |
| `candidates_total` | count | how many listings go to the matcher |
| `candidate_checking` | url, domain, title, price, index, total | the matcher started on one listing |
| `candidate_verdict` | url, domain, is_match, confidence, reason, price | the LLM's verdict |
| `candidate_gate_rejected` | url, reason | a safety check overruled a match (price far from GeM) |
| `candidate_skipped` | url, reason | could not be judged (fetch failed, LLM down) |
| `retry_plan` | new_queries, notes | nothing matched, trying other search words |
| `llm_call` | endpoint, provider, model, purpose, seconds, ok, status, error | one attempt on one LLM endpoint (`purpose` is normalize, verify or expand) |
| `llm_cache_hit` | purpose | an answer came from the saved-answer cache (`LLM_CACHE=1`), no LLM call was made |
| `llm_cooldown` | endpoint, seconds, reason | an endpoint is resting after an error |
| `llm_disabled` | endpoint, reason | an endpoint was switched off (bad key, unknown model) |
| `run_finished` | from_cache, matches, cheapest, savings, trace_id | the run is over |
| `run_failed` | error | the run crashed |

Rules the frontend can rely on:

- Events arrive in `seq` order and none is skipped.
- A `stage` event ends the previous stage, so a step's duration is the
  difference between two `t` values.
- The same listing can be checked twice (the retry re-checks everything).
  The later `candidate_verdict` wins.
- New event types may appear later. Ignore the ones you do not know.

The final comparison is in `GET /api/runs/{id}` under `result`, with the same
shape `run_pipeline` has always returned.
