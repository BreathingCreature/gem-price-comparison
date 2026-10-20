# Architecture

```
                 +-----------------------------+
cli.py  -------->|                             |------> scrapers/gem.py      (GeM page)
api/server.py -->|      core/pipeline.py       |------> scrapers/amazon.py   (Amazon.in)
frontend/ <------|      run_pipeline()         |------> scrapers/flipkart.py (Flipkart)
   ^             +-----------------------------+------> scrapers/generic.py  (any shop URL)
   |                          |              |
   |                          |              +------> discovery/discover.py (find other shops)
   |                          |
   |                   core/events.py  (progress events)
   |                          |
   +---- server-sent events --+------> llm/client.py -> llm/router.py -> provider A/B/C
```

`core/pipeline.py` is the only thing that decides how a comparison runs. The
CLI, the API and the evaluation all call `run_pipeline`. Helpers live next to
it: `core/candidates.py` (candidate rows and de-duplication),
`core/searches.py` (the shop searches), `core/prefilter.py` (the cheap filter),
`core/result.py` (result v2), `core/retry.py` and `core/similar.py`.

## The flow

1. **Read the GeM page** (`scrapers/gem.py`). Title, price (fixed or a range,
   `low`/`high`), brand, model, specs. `gem_product` event.
2. **Understand the product** (`llm/client.py` normalize). Canonical name,
   brand, model, search words. If the LLM is unavailable, a plain-text
   fallback is used and the run is marked (`_identity_without_llm`).
3. **Three lanes start together** when `GEM_PARALLEL=1` (the default):
   - Flipkart search,
   - Amazon search,
   - discovery (search engines) + one page per other shop.

   Each lane is wrapped: a crash becomes a `lane_failed` event and an empty
   lane, not a dead run. Only if all three crash does the error reach the user.
4. **Filter, then verify.** Each candidate goes through `core/prefilter.py`
   (model number, brand, word overlap) and is dropped only if it is clearly not
   the product. The rest goes to the LLM verifier through
   `llm/match.py` with sanity gates: non-INR prices, price far outside the GeM
   band, confidence below 0.75. Outcomes are `match`, `possible` and
   `not_checked`; they are kept apart so the page never calls a guess a match.
5. **First result.** As soon as the lanes settle (or a lane hits its time
   limit, `GEM_LANE_TIME_LIMIT`), shops are summarised, `run_finished`
   (`state: "shown"`) is emitted and the run record is saved.
6. **One retry.** `core/retry.py` picks the shops that said
   `retryable: true` (blocked, incomplete, timed out, or nothing found) and
   asks the LLM for fresh search words. `retry_started`, then `shop_updated`
   for the shops whose card really changed, then `run_updated`.
7. **Similar products.** Only when there is no confirmed match does
   `core/similar.py` look for near misses, so the page has something useful to
   show. `similar_ready`.
8. **Settle.** Final result v2 is cached (unless the cache was bypassed or
   disabled), written to a trace, saved as a run record, and `done` is sent.

## Shop statuses

Every shop card has one of: `found`, `possible`, `not_found`, `not_searched`
(blocked or skipped before it could search), `incomplete` (ran but lost
listings), `timed_out`. Only the unfinished ones are retried, and only once.
A lane that *crashed* is not retryable, so a broken shop is not asked again.

## Result v2

`core/result.py::build_result` builds one object for the CLI, the API, the
traces and the saved runs. It is versioned (`RESULT_VERSION = 2`); the cache
key includes the version, so an upgrade never serves an old shape. Legacy keys
(`gem_product`, `not_found_on`, `comparison`, ...) are still written for old
tools. The full shape is in [api.md](api.md).

## Events

`core/events.py` is a tiny emitter. The pipeline narrates; listeners are
optional and cheap. The API forwards events to the browser and saves them in
the trace. Event names are listed in [api.md](api.md); the frontend ignores the
ones it does not know.

## LLM gateway

`llm/router.py` owns provider choice, cooldowns, daily limits, per-minute token
budgets and the "used up for today" state. `llm/client.py` builds prompts and
parses answers; `llm/match.py` keeps the gates between the model and the user.
See [llm-providers.md](llm-providers.md).

## Frontend

Plain HTML, CSS and JavaScript, no build step. `frontend/render.js` is a
DOM-free formatting module (money, shop wording, link safety) so it can be
tested with Node; `frontend/app.js` only touches the DOM. `window.GEM_API_BASE`
points the page at another server.

## Storage

- `cache/` — one JSON per URL, TTL `CACHE_TTL_SECONDS`.
- `traces/` — one JSON per run, compact, rotated by count and age.
- `cache/runs/` — one small record per finished run, so the recent list and a
  saved result survive a restart.
- `debug_html/` — only written by a failed live doctor canary or a scraper
  debug path.

## Why the pipeline file is still big

The lanes, the cache and the trace writer stay in `core/pipeline.py` because
tests patch them there (`pipeline.PREFILTER_TOP`, `pipeline.CACHE_DIR`,
`pipeline.TRACES_DIR`). Moving them would mean rewriting those patch points for
no visible gain. The search and candidate code already moved to
`core/searches.py` and `core/candidates.py`. See `docs/DECISIONS.md`.
