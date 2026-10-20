# Decisions

- 2026-10-20 - The `spec/` folder named in MASTER_PROMPT does not exist in the
  repo. Used `MASTER_PROMPT.md` plus a direct reading of the code as the design
  source. (Stage 1)

- 2026-10-20 - One listing key for everyone: `core/keys.py::listing_key`.
  The pipeline's `_listing_key` and the scorer's `url_key` now delegate to it.
  Amazon collapses to `amazon.in/dp/<asin>`; Flipkart to
  `flipkart.com?pid=<pid>&lid=<lid>`; other shops keep host + path + sorted
  non-tracking parameters. (Stage 2)

- 2026-10-20 - Flipkart's `lid` counts as a real variant, as the master prompt
  says, so two URLs with the same pid but different lid are now different
  listings. Two old pipeline tests asserted the opposite (pid-only dedup);
  they were rewritten, not deleted. (Stage 2)

- 2026-10-20 - The scorer used to drop the whole query string for "other
  shops". It now keeps real parameters (`page`, `color`, ...) and drops only
  tracking ones, matching the pipeline. `tests/test_eval_score.py` was updated
  to the new key shape. (Stage 2)

- 2026-10-20 - `GEM_TRACE_MAX_AGE_DAYS=0` means "no age limit"; the count cap
  still applies. (Stage 2)

- 2026-10-20 - Run records live in `cache/runs/<run_id>.json` (one small JSON
  per run). `list_runs()` sorts by `saved_at`, so list order never depends on
  the filesystem. (Stage 2)

- 2026-10-20 - Shops report why they came back empty: SearchScrapeResult
  carries status (ok | locked | parser_empty) and 
eason. A page
  with zero cards and no no-results marker is parser_empty, never "no
  products"; a CAPTCHA is locked. error also gets a short text for
  parser_empty so old callers that only read error still see it. (Stage 3)

- 2026-10-20 - Flipkart's fast path is on by default (GEM_FLIPKART_LIGHT,
  now True): one plain-HTTP GET of the search page, used only when it looks
  like a real results page (the genuine no-results marker or at least three
  parsed cards). One fingerprint, not a rotation — the point is one cheap
  request before deciding on Chrome. A later page failing only stops paging;
  a page-1 failure hands the whole query to Chrome. A dead Chrome is rebuilt
  once per run, not once per query. (Stage 3)

- 2026-10-20 - The struck-out MRP containers are removed before any price
  read on a Flipkart card, not just before the text fallback, so a drifted
  selling-price class cannot make the MRP look like the price. Amazon reads
  span.a-price without .a-text-price for the same reason. Both shops mark
  a card in_stock: False when its text says so; unknown means in stock.
  (Stage 3)

- 2026-10-20 - Per-page card cap: there was none to remove. Every card on a
  page was already parsed; only the run's total max_results caps a page.
  Verified rather than invented, so no cap was added. (Stage 3)

- 2026-10-20 - The generic scraper reads JSON-LD (Product, @graph, lists,
  AggregateOffer.lowPrice) before meta tags, then h1/title. A listing with a
  title but no price is kept with price: null + price_unknown: true;
  soft-404 pages carry page_not_found: true. Non-INR currencies are still
  kept as-is so the matcher rejects them. (Stage 3)

- 2026-10-20 - Generic scraping refuses non-public addresses before the
  request and again after redirects (_is_public_http_url). The guard is
  scheme + IP-literal based; a hostname that resolves to a private address is
  left to requests, because resolving it in the guard would duplicate DNS and
  still race. (Stage 3)

- 2026-10-20 - GeM products carry `price = {low, high, kind}` (kind fixed |
  l1 | range | unknown) plus the selector that produced it. gem_price stays
  the low end and gem_price_type stays for old callers. Brand/model fall
  back to the spec table when the title has neither. (Stage 3)

- 2026-10-20 - undetected-chromedriver is gone, not just optional: the code
  path, the config switch and the requirements comment were removed. Plain
  Selenium + the CDP mask stays. (Stage 3)

- 2026-10-20 - Tests hide `curl_cffi` from `sys.modules` (the autouse
  fixture) and the Flipkart/fast-path seams, so no test can reach a live shop
  through libcurl, whose sockets bypass the socket-level network guard.
  tests/test_doctor.py removes that sentinel for its package check only.
  (Stage 3)

- 2026-10-20 - Discovery asks every engine at the same time and takes the
  union, deduped by `core/keys.py::listing_key`. One deadline covers the
  whole call (`GEM_DISCOVERY_TIME_LIMIT`, now 25 s, was a 20 s SearXNG-only
  budget); engines still running at the deadline are cancelled and noted.
  (Stage 4)

- 2026-10-20 - Engine health is in memory only: three failures in a row skip
  that engine for five minutes, a success resets the count, and a timeout
  counts as a failure. A restart retries everything, which is the right
  default after a network blip. (Stage 4)

- 2026-10-20 - `ddgs` is an optional guarded import and the engine is left
  out entirely when the package is missing (no issue line on every run).
  `duckduckgo` and `searxng` stay the two always-tried engines; the
  six-backend router from the spec is not built. (Stage 4)

- 2026-10-20 - `discovery/indian_shops.txt` is additive to the built-in
  Indian shop list, one domain per line, comments allowed. `.in` /
  `.co.in` still pass without being listed. (Stage 4)

- 2026-10-20 - `DiscoveryResult` gained `search_issues` (one line per
  failed, skipped or timed-out engine), included in `to_dict` so it lands
  in the trace. It is not folded into the run's `search_issues`: doing so
  would make every run "uncacheable" whenever an engine hiccups. Blocked is
  reported only when no engine produced links and at least one was blocked;
  otherwise the call reports the engine errors. (Stage 4)

- 2026-10-20 - "Used up for the day" is a persisted fact, not a cooldown.
  `cache/llm_state.json` holds `{endpoint name: epoch second it may be tried
  again}` and nothing else — no keys, no prompts, no answers. It is written
  atomically (temp file + `os.replace`) and read when a `Router` starts, so
  a restart after "daily limit" still skips that endpoint. Entries expire on
  their own; a successful call elsewhere does not clear them. (Stage 5)

- 2026-10-20 - A quota answer is recognised by status/text, not by every
  429: HTTP 402, or text containing "daily limit", "daily quota", "quota
  exceeded", "exceeded your quota", "quota exhausted", "insufficient_quota",
  "out of credits" or "credit balance". A plain 429 ("slow down") keeps the
  ordinary short cooldown; treating one burst as a lost day would waste free
  capacity. When the error text says when it resets ("retry after 2 hours",
  "resets in 45s") that time is used; otherwise it is the next local
  midnight. (Stage 5)

- 2026-10-20 - `GatewayBusy` and `GatewayExhausted` share the `GatewayError`
  base, which subclasses `RuntimeError`, so older `except RuntimeError`
  callers (including the matcher's per-listing skip) keep working unchanged.
  `GatewayExhausted`'s message is the pipeline warning verbatim — "LLM
  limits used up until HH:MM for: name, name"; endpoints stopped by a
  configured `daily_limit` add their call counts in parentheses. (Stage 5)

- 2026-10-20 - The pipeline does not die when the LLM is gone: normalize
  falls back to the GeM title as the identity, and every match batch is
  wrapped so a busy/used-up gateway turns that batch's listings into skipped
  (reported as "verify incomplete") with one warning. The full
  `not_checked` status belongs to Stage 6; this stage only keeps the run
  alive and honest. (Stage 5)

- 2026-10-20 - An empty answer is retried once on another endpoint and then
  raises `GatewayBusy` with the last failure in the message. It is never
  cached, because only parsed JSON reaches `llm/cache.py::put` and an empty
  answer never gets that far. (Stage 5)

- 2026-10-20 - Describe prompt v2 adds `aliases` and `key_attributes` (both
  optional lists, parsed with the existing defensive list coercion) and the
  verify prompt states the model-code / pack-size / missing-price rules in
  plain words. Page text stays inside the same fences, defused the same way.
  (Stage 5)

- 2026-10-20 - The pre-filter is ON by default at ten listings per shop
  (`GEM_PREFILTER_TOP=10`) and has hard ceilings regardless of the setting:
  at most 10 non-model-code listings per shop, and at most 20 model-code
  hits (the ones priced nearest the GeM price win above that). If the caps
  would leave a shop with nothing, its best three by score are sent anyway
  (the recall safety net). Every dropped listing is in the trace as
  {title, url, price, score, tier, rule}, where rule is one of
  over_shop_cap, over_model_cap, not_relevant. A product with no brand and
  no model code gets the lower 0.4 relevance bar (`min_relevant_score`),
  because it has fewer words to share with a real title. (Stage 6)

- 2026-10-20 - `GEM_STRICT=1` is the escape hatch, not the default: the
  pre-filter is off (every listing reaches the LLM) and the verifier runs
  one listing at a time, while the searches still run as lanes
  (`PARALLEL_SEARCH or STRICT_MODE`). That keeps the old behaviour one
  switch away without making it the normal path. (Stage 6)

- 2026-10-20 - Every judged listing gets one outcome: `match` (confirmed
  with a real price), `possible` (the same product may be right, never used
  for cheapest/savings), `no_match`, or `not_checked` (the gateway could not
  answer). `possible` carries a short `why`: "different pack", "price
  unknown", or "confidence 0.60 below 0.75". A same-product verdict with no
  price is `possible`, replacing the old silent demotion to skipped.
  `not_checked` listings are also appended to `skipped` so every caller
  written before this stage still sees them as "no verdict"; Stage 7 splits
  the two counts. (Stage 6)

- 2026-10-20 - Verdict reuse is ON: one answer covers the same (canonical
  name, model, GeM price, listing title, listing price). Within a batch the
  duplicates wait for the first answer; across shops the pipeline passes one
  shared `verdict_cache` dict, so a Flipkart listing copied into discovery
  costs nothing. A reused decision keeps the current listing's URL and
  domain, so a cross-shop reuse can never show the wrong link. The verifier
  runs 4 listings at a time by default (`GEM_LLM_WORKERS`). (Stage 6)

- 2026-10-20 - The scraper's card price is the price. When the LLM's
  extracted price disagrees, the decision keeps the scraper value with
  `price_from: "scraper"`, and records `llm_price_seen` plus a `price_note`
  instead of hiding the disagreement. The price pre-gate is now ON by
  default. A page showing only an MRP cannot be detected yet (no scraper
  flag distinguishes MRP from selling price), so it is treated like any
  other price; when the price is missing the listing becomes `possible`
  with why "price unknown". (Stage 6)

- 2026-10-20 - Similar products are one LLM call, made only when the run
  found neither a match nor a possible one. It sees up to five tier-2/3
  listings that share product words (tier 1 is the same-product question,
  already answered) and returns [{shop, title, price, url, why}] cheapest
  first. A failure is a warning, never an error, and `similar` never feeds
  the cheapest/savings arithmetic. (Stage 6)

- 2026-10-20 - Lanes are the default (`GEM_PARALLEL=1`); `0` keeps the old
  sequential path for tests and strict debugging. Each lane ends with a
  `shop_finished` event carrying its card, and the run emits a first,
  complete result (`state: shown`) before any retry, so the page never waits
  for a slow shop to show what the healthy ones already found. (Stage 7)

- 2026-10-20 - A shop card is honest about what happened: `found` needs at
  least one confirmed match; a blocked or empty search is `not_searched`; a
  listing the LLM could not judge is `incomplete` (never `not_found`); a
  lane over `GEM_LANE_TIME_LIMIT` (150 s) is `timed_out`; possible-only is
  `possible`. `retryable` is true only for statuses a second attempt could
  change, and a lane that crashed is not retried (the same call would crash
  again). `build_result` enforces the same rules a second time, including
  "no cheapest without a priced in-stock match and a GeM price". (Stage 7)

- 2026-10-20 - Retry is per shop, not per run: after the first result only
  the shops whose card says `retryable` are searched again, with the new
  words from one `expand` call, and at most once per shop. A retry caused by
  one unfinished shop keeps the matches another shop already confirmed
  (merging, not replacing). If the LLM is used up, the retry is skipped with
  a warning. A crashing retry search or discovery call is logged and the
  first result stands. (Stage 7)

- 2026-10-20 - The result object is built in one place (`core/result.py::
  build_result`) and gains `result_version: 2`, `run_id`, `checked_at`,
  `state`, `gem`, `shops`, `cheapest` (or null), `compared_with` and
  `described_by`. Old top-level keys (`gem_product`, `not_found_on`,
  `search_issues`, `skipped`, `comparison`) stay for this release so the
  CLI, evaluator and current page keep working; changing `RESULT_VERSION`
  also invalidates old cache files, so a v1 entry is never read as v2.
  (Stage 7)

- 2026-10-20 - Similar products and the retry decision live in their own
  modules (`core/similar.py`, `core/retry.py`) so both can be tested without
  a pipeline run. `pipeline._similar_products` stays as a thin alias because
  existing tests patch it. (Stage 7)

- 2026-10-20 - Stage 6's A11 (never retry a blocked shop) is replaced by the
  spec's rule: a blocked shop is `not_searched` with `retryable=True` and
  gets exactly one retry, because a different query can reach a page that
  was CAPTCHA'd for the first one. `test_a_blocked_shop_is_not_asked_again_
  on_the_retry` was rewritten as `test_a_blocked_shop_is_retried_once_with_
  the_new_words`. (Stage 7)

- 2026-10-20 - The search half of the pipeline now lives in `core/searches.py`
  (direct search, search health, discovery + generic scraping) and the pure
  candidate helpers in `core/candidates.py`; the pipeline re-exports the old
  names so tests and tools calling `pipeline._search_direct`, `_dedup`,
  `_write_trace` keep working. `core/pipeline.py` is still above the 500-line
  guideline (cache, trace rotation and the lane runner stay there on purpose:
  existing tests patch `pipeline.CACHE_DIR`, `pipeline.PARALLEL_SEARCH` and
  `pipeline.PREFILTER_TOP`, which only works while those names are read from
  that module). Splitting the rest safely is Phase 2 work, not a behaviour
  change to slip in here. (Stage 7)

- 2026-10-20 - `GEM_LANE_TIME_LIMIT` marks a shop `timed_out` but cannot stop
  the lane's thread: the run stops waiting on that lane's answer by keeping
  the rest of the result and writing the reason on the card. The retry then
  gives that shop one more try. (Stage 7)

## Stage 8 - interfaces (API, demo, frontend, CLI)

- **Recent runs are merged from memory and disk.** `GET /api/runs` lists live
  jobs first and then the saved run records, and `GET /api/runs/{id}` falls
  back to `runs_store.get_run` when the job is gone. A run record now stores
  the whole v2 result, so a restart does not lose the page you were looking
  at; the cost is a slightly larger file per run, which the rotation already
  bounds.
- **The demo runs the real pipeline with fakes.** `api/demo.py` builds a fake
  GeM product, fake shop searches and a fake matcher, then calls
  `run_pipeline(..., use_cache=False)`. The page therefore sees the same event
  order as a real run (including the per-shop retry), and a demo residue can
  never be served from the cache. `use_cache` is a pipeline keyword, off only
  for the demo.
- **Trace text is trimmed before it leaves the API, not when written.** The
  saved trace keeps everything for debugging; the `/api/traces/{id}` route cuts
  known page-text fields to 1000 characters unless `GEM_DEBUG=1`. Trimming at
  write time would make the trace useless for the one thing it is for.
- **Formatting lives in `frontend/render.js`, not in `app.js`.** The DOM-free
  module is required by the page and by a Node smoke test, so money, shop
  wording and link safety are testable without a browser. The page scripts
  still use no `innerHTML` anywhere.
- **The CLI keeps watching after the first result, with a time limit.** After
  `run_finished(state="shown")` the printer keeps printing `shop_updated` and
  `similar_ready` lines, then `Done.`. It stops after `GEM_RETRY_CLI_WAIT`
  seconds (default 120, the pipeline retry budget) with one line saying so, so
  a terminal is never held open forever. `--no-wait` turns the watching off.
- **A link that is not GeM exits 2 before anything runs.** Both the API and the
  CLI use `core/gem_url.looks_like_gem_url`; the CLI prints one line to stderr
  and does not import the pipeline (so `--strict` can set `GEM_STRICT` first).

## Stage 9 - tools, CI, docs, cleanup

- **`--live` is one switch, the old single checks stay.** The owner runs
  `tools/doctor.py --live` and sees LLM endpoints, search engines and shop
  canaries in one go; `--live-llm`, `--live-engines`, `--live-shops`,
  `--live-scrape` and `--live-failover` still work so a single failing part
  does not force a full live run. A failed canary saves the page under
  `debug_html/` and prints the path, because a CAPTCHA page and a changed
  layout need different fixes.
- **`--clean` is opt-in.** Rotation already bounds `traces/` and
  `cache/runs/`; `--clean` exists for a manual tidy-up and uses
  `GEM_TRACE_MAX_AGE_DAYS` unless `--older-than` says otherwise. Deleting
  files is never part of a normal check.
- **`requirements.txt` is fully pinned; `ddgs` became a comment.** Every
  installed version comes from the environment where the suite is green. The
  `ddgs` discovery engine is imported defensively and is not needed for tests,
  and pinning a version that was never installed would be a guess, so it is a
  commented opt-in line instead.
- **CI runs Windows and Linux on Python 3.12.** Windows is the owner's
  machine; Linux catches accidental path assumptions. `compileall` runs after
  the suite so a module that no test imports still gets byte-compiled.
- **`tools/try_flipkart_fast.py` was deleted.** Its question ("can Flipkart
  search be fetched without a browser?") is answered in the scraper now
  (`_fetch_light_page`, `_light_page_usable`, `_search_light`), and the tool
  was an experiment that could drift away from the real path.
- **`docs/fast-profile.md` merged into `docs/settings.md`.** Every switch it
  recommended is now a default, so a separate page would have told the reader
  to turn on what is already on. `docs/settings.md` is now the one table for
  every setting.
- **`scrapers/__init__` no longer uses `eval(name)`.** The lazy GeM exports
  are looked up with `getattr` on the module, so a module attribute name can
  never reach the built-in evaluator.
- **Old `Part N` / `kit N` docstring labels were replaced with plain names.**
  They described the build order, not the code, and a reader without that
  history learned nothing from them. A double-encoded em dash in
  `core/pipeline.py` (from an old PowerShell edit) was repaired at the same
  time.
- **The evaluation reader accepts v1 and v2 matches.** `eval/run_eval.py`
  reads the v1 keys and falls back to the v2 names (`url`/`shop`/`price`), so
  old `eval/results/*.jsonl` files still score.
