# API and event contract

The frontend only knows this page. Anything not described here is an internal
detail and may change.

Start the server: `python -m uvicorn api.server:app --port 8000`, then open
http://localhost:8000. The server is meant for your own machine, so it binds
`127.0.0.1` by default; pass `--host 0.0.0.0` only if you really want it on the
network. Set `GEM_DEMO=1` first to run a fake pipeline (no scraping, no LLM
calls), which is the easy way to work on the frontend.

## Routes

| Method and path | What it does |
|---|---|
| `GET /api/health` | `{"ok": true, "demo": false}` |
| `POST /api/runs` | Body `{"gem_url": "...", "refresh": false}`. Starts a run, answers `202 {"run_id", "status"}`. Answers 422 if the link is not a gem.gov.in link (demo mode accepts any link). |
| `GET /api/runs` | The 20 most recent runs, newest first. Live jobs come first, then the runs saved on disk, so the list survives a restart. Each item has `id`, `gem_url`, `title`, `state`, `status`, `cheapest_price`, `match_count`, `checked_at`, `from_cache`, `live`. |
| `GET /api/runs/{id}` | A live job answers with `status`, `state` (`shown`, `done`, `failed`), timestamps, `result`, `error`, `live: true`. A finished run whose job is gone is read from disk (`live: false`), so a result can still be opened after a restart. 404 when neither knows the id. |
| `GET /api/runs/{id}/events` | Server-sent events. Replays every event from the start, then follows the live run. `?after=N` (or the `Last-Event-ID` header) continues after event number N. Ends with `event: done` and `{"status": "done"}` or `"failed"`. |
| `GET /api/llm/status` | The LLM router's view: strategy and, per endpoint, state (`ready`, `cooling`, `disabled`, `limit`), calls, errors, average seconds, last error. `limit` means the endpoint used up its allowance for today; the state survives a restart. |
| `GET /api/traces/{trace_id}` | The saved trace JSON of a finished run (it includes every event). Long page text is cut to 1000 characters with a `… (trimmed)` suffix; set `GEM_DEBUG=1` on the server to get the raw text. |

Only one run executes at a time by default (set `GEM_JOBS=2` to let two run together — Chrome and the shops tolerate that, not five). Others wait in `queued`.

## Events

Every event is one JSON object: `seq` (1, 2, 3 ...), `t` (seconds since the
run started), `type`, plus the fields below.

| type | fields | meaning |
|---|---|---|
| `run_started` | gem_url, refresh | the run began |
| `cache_hit` | gem_url | a saved result is being reused (run ends right after) |
| `stage` | name, label | a new step began (the previous one is over) |
| `gem_product` | title, price, price_type, brand, model, category, specs | the GeM page was read |
| `product_ready` | title, price | the reference product is known; shops can start |
| `normalized` | canonical_name, brand, model, queries, key_identifiers | the LLM's understanding of the product |
| `search_started` | marketplace, query | a Flipkart or Amazon search began |
| `search_finished` | marketplace, query, count, blocked, error, candidates[] | results; each candidate has title, price, url, domain, error |
| `search_health` | engine, ok, reason | a discovery engine worked or failed |
| `discovery_finished` | queries, urls, issues | other shop pages worth opening |
| `scrape_started` / `scrape_finished` | url / title, price, url, domain, error | one of those pages was opened |
| `candidates_total` | count | how many listings go to the matcher |
| `candidate_checking` | url, domain, title, price, index, total | the matcher started on one listing |
| `candidate_verdict` | url, domain, is_match, confidence, reason, price | the LLM's verdict |
| `candidate_gate_rejected` | url, reason | a safety check overruled a match (price far from GeM) |
| `candidate_skipped` | url, reason | could not be judged (fetch failed, LLM down) |
| `retry_plan` | new_queries, notes | nothing matched, trying other search words |
| `llm_call` | endpoint, provider, model, purpose, seconds, ok, status, error | one attempt on one LLM endpoint (`purpose` is normalize, verify, expand or similar) |
| `llm_cache_hit` | purpose | an answer came from the saved-answer cache (`LLM_CACHE=1`), no LLM call was made |
| `llm_cooldown` | endpoint, seconds, reason | an endpoint is resting after an error |
| `llm_disabled` | endpoint, reason | an endpoint was switched off (bad key, unknown model) |
| `shop_started` | shop | a shop (amazon.in, flipkart.com or other) entered the first pass |
| `shop_finished` | shop, status, reason | that shop's first pass is over; `status` is one of found, possible, not_found, not_searched, incomplete, timed_out |
| `run_finished` | from_cache, state, result, matches, cheapest, savings, trace_id | the first honest result is ready (`state: "shown"`), or a cached run is being replayed. `result` is the whole v2 object below. |
| `retry_started` | shops[] | exactly one retry began for the shops that said they were worth asking again |
| `shop_updated` | shop, status, price | the retry changed a shop's card |
| `run_updated` | shops[], matches[], cheapest | the merged result after the retry |
| `similar_ready` | similar[] | near-miss products found after the confirmed matches were settled |
| `done` | state | nothing more will arrive; the connection closes |
| `run_failed` | error | the run crashed |

Rules the frontend can rely on:

- Events arrive in `seq` order and none is skipped.
- A `stage` event ends the previous stage, so a step's duration is the
  difference between two `t` values.
- The same listing can be checked twice (the retry re-checks everything).
  The later `candidate_verdict` wins.
- A `shop_updated` is only sent for a shop the retry actually changed.
- After `run_finished(state: "shown")` the page is allowed to show a complete
  look; `run_updated` and `done` replace it when the retry lands.
- New event types may appear later. Ignore the ones you do not know.

## The result object (version 2)

`GET /api/runs/{id}` and `run_finished` return the same object:

```json
{
  "result_version": 2,
  "state": "done",
  "from_cache": false,
  "checked_at": "2026-10-20T10:00:00+00:00",
  "trace_id": "abc123",
  "gem": {"title": "...", "price": {"low": 1299.0, "high": 1299.0, "kind": "fixed"}},
  "shops": {
    "flipkart.com": {"status": "found", "reason": "", "seen": 3, "dropped": 1, "checked": 2,
                     "matches": 1, "price": 1149.0, "retried": true, "retryable": false, "seconds": 6.2}
  },
  "matches": [{"shop": "flipkart.com", "title": "...", "price": 1149.0, "confidence": 0.94,
               "reason": "...", "url": "https://...", "in_stock": true}],
  "possible_matches": [{"shop": "amazon.in", "title": "...", "price": 1249.0, "why": "..."}],
  "similar": [{"source_domain": "...", "title": "...", "price": 999.0, "candidate_url": "https://..."}],
  "cheapest": {"shop": "flipkart.com", "price": 1149.0, "saving_vs_gem": 150.0},
  "warnings": [],
  "not_found_on": [],
  "search_issues": [],
  "skipped": [],
  "comparison": {"gem_price": 1299.0, "cheapest_match_price": 1149.0, "savings_vs_gem": 150.0}
}
```

The legacy keys (`gem_product`, `not_found_on`, `search_issues`, `skipped`,
`comparison`) are kept so older tools keep working. `None` means unknown and is
never turned into a guessed number.
