# Final report

All nine stages are done, merged to `main`, and pushed. The suite ends at
**701 passing tests**. No live network run was done.

## 1. Stages

| Stage | Branch | Merge commit | Test functions added | Suite after merge |
|---|---|---|---|---|
| 1 Safety | `stage-1-safety` | `83edb8a` | 5 | 479 |
| 2 Foundations | `stage-2-foundations` | `576dcf2` | 29 | 532 |
| 3 Shops | `stage-3-shops` | `15aa697` | 43 | 571 |
| 4 Search | `stage-4-search` | `39a6e2b` | 12 | 583 |
| 5 LLM gateway | `stage-5-llm` | `4690a59` | 21 | 604 |
| 6 Matching | `stage-6-matching` | `26a65a6` | 36 | 638 |
| 7 Pipeline | `stage-7-pipeline` | `91b689c` | 26 | 661 |
| 8 Interfaces | `stage-8-interfaces` | `fea6dbf` | 37 | 691 |
| 9 Tools/docs | `stage-9-tools-docs` | `1f590a9` | 14 | 701 |

"Added" counts test functions in the diff, so a rewritten test shows as one
removed and one added. The suite column is the real pytest count after the
merge.

## 2. What each stage changed

1. **Safety.** The generic scraper no longer crashes on a real `requests`
   response; fakes in tests now look like real responses; a network guard
   makes any accidental real request fail the test.
2. **Foundations.** One shared listing key, secret redaction everywhere, an
   on-disk run index, a cache key that includes the result version, and
   age-based trace rotation.
3. **Shops.** Every shop has a status and a reason, Amazon reads the selling
   price and stock, Flipkart splits MRP from price and has a light HTTP path,
   the generic scraper reads JSON-LD and refuses private hosts, and the GeM
   price can be a range.
4. **Search.** Engines asked in parallel, a failing engine is skipped for a
   while, results carry `search_issues`, Indian shops get an allow-list, and
   discovery has a time budget.
5. **LLM gateway.** "Used up for today" survives a restart, gateway errors are
   typed, prompts are v2, and a run keeps going when the LLM is unavailable.
6. **Matching.** The filter is on by default, every listing gets an outcome
   (`match` / `possible` / `no_match` / `not_checked`), verdicts are reused,
   the scraper price wins, and similar products are found only when nothing
   matched.
7. **Pipeline.** The first result is shown early, every shop has an honest
   card, one retry per shop, result v2, similar products, and a lane time limit.
8. **Interfaces.** Recent runs and saved results survive a restart, traces are
   trimmed for the API, the demo runs the real pipeline with fakes, the page
   and CLI were rewritten, and the CLI keeps printing updates with a wait limit.
9. **Tools/docs.** `doctor.py --live` and `--clean`, `tools/save_page.py`,
   finish reason and rate-limit header *names* in the probe, CI on Windows and
   Linux, pinned requirements, rewritten README and docs, dead code removed.

Old tests rewritten, and why:

- Stage 1: scraper and pipeline fakes changed from dicts to real response-like
  objects, because the old fakes hid the crash.
- Stage 2: the eval scorer was changed to use the shared listing key, because
  two different URL rules disagreed.
- Stage 3: scraper and pipeline tests changed for prices as ranges and for
  shop statuses; a price is no longer a bare float.
- Stage 4: discovery tests changed for the parallel engine API and for
  `search_issues`; the sequential fetcher is gone.
- Stage 5: LLM client/router tests changed for the gateway, the typed errors
  and prompt v2.
- Stage 6: matcher and pipeline tests changed for outcomes and "possible"
  instead of a boolean match.
- Stage 7: pipeline, event, run-flag, gateway and client tests changed for the
  early `run_finished(state: "shown")`, shop cards and result v2.
- Stage 8: CLI, frontend, API and doctor tests changed for the new page, the
  new CLI flags, saved runs and the demo that runs the real pipeline.
- Stage 9: probe tests changed for the extra row fields; the Flipkart
  experiment test was deleted together with its tool.

## 3. Failed before, passes now

Each row was checked in a temporary git worktree at the commit **before** the
change. The current test file was copied in and run there.

| Test | Before | Now |
|---|---|---|
| `test_generic_scrape_works_with_a_real_response_object` (real response) | FAILED (`AttributeError` on the fake-shaped response) | PASS |
| `test_key_shaped_text_is_hidden` (redaction) | test file could not even be collected (`core.errors` did not exist) | PASS |
| `test_the_true_long_title_reaches_the_llm_from_position_thirty_one` (recall safety) | 3 of 3 recall tests FAILED | PASS |
| `test_a_crashing_lane_does_not_kill_the_other_lanes` (lane crash) | FAILED (the crash killed the run) | PASS |
| `test_a_normal_run_reports_every_step_in_order` (event order) | FAILED (no such event flow) | PASS |

Whole-stage proofs, same method:

- Stage 7: 23 new/changed tests failed at `26a65a6`.
- Stage 8: 27 failed at `91b689c`, plus `test_frontend.py` could not be
  collected because `frontend/render.js` did not exist yet.
- Stage 9: 16 failed at `fea6dbf` (doctor live/clean, save_page, probe fields).

## 4. Settings whose default changed

| Setting | Old | New | Why |
|---|---|---|---|
| `GEM_PARALLEL` | off | **on** | the three lanes are the normal flow now |
| `GEM_PREFILTER_TOP` | `0` (off) | **`10`** | filter on by default, with caps and a safety net |
| `GEM_LLM_WORKERS` | `1` | **`4`** | four listings verified at once |
| `GEM_PRICE_PREGATE` | off | **on** | no LLM call for an impossible price |
| `GEM_REUSE_VERDICTS` | off | **on** | do not ask the same listing twice |
| `GEM_FLIPKART_LIGHT` | off | **on** | plain HTTP search first |
| `RESULT_VERSION` | `1` | **`2`** | shop cards, matches, possible, similar |
| `GEM_LANE_TIME_LIMIT` | (none) | **`150`** | a lane cannot hold the first result forever |
| `GEM_DISCOVERY_TIME_LIMIT` | `20` | **`25`** | all engines share one budget |
| `GEM_TRACE_MAX_AGE_DAYS` | (none) | **`14`** | rotation by age as well as count |
| undetected-chromedriver | optional switch | **removed** | it was unreliable on current Chrome |

Everything else keeps its old default. `GEM_STRICT` is still off.

## 5. Done differently, and not done

- **`spec/` does not exist in the repository.** The master prompt was the
  authority; this is recorded in `docs/DECISIONS.md`. The "Known limits" list
  in the README was written from the code, not copied from a missing file.
- **Stage 6 A11 ("never retry a blocked shop") was replaced by stage 7.2.**
  A blocked shop is `not_searched`, `retryable: true`, and gets exactly one
  retry. A lane that *crashed* is still never retried.
- **The lanes and the cache/trace writer stay in `core/pipeline.py`.** The
  search half and the candidate helpers moved out, but tests patch
  `pipeline.PREFILTER_TOP`, `pipeline.CACHE_DIR` and `pipeline.TRACES_DIR`;
  moving those would only move the patch points. Noted as later work.
- **`ddgs` is a commented line in `requirements.txt`.** It is optional and was
  not installed here, so pinning a version would be a guess.
- **No live runs.** The Dump2 live items (answer-key accuracy, C10, A3/A4/A8/A9,
  H/I/J, D4) still need the owner's `eval/answer_key.csv` and a real network.
- **`docs/review/` stays untracked on disk.** It was never part of the repo.
- **`AGENTS.md` is local memory** (git-ignored). It still says 474 tests; the
  real number is 701.

## 6. Log and count

```
1f590a9 Merge stage-9-tools-docs: live doctor, save_page, pinned requirements, CI and rewritten docs
b59fd1a Rewrite the docs for the new flow and remove dead code and stale labels
b4800de Add live doctor checks, page saving, pinned requirements and CI
fea6dbf Merge stage-8-interfaces: saved runs after restart, trimmed traces, new page and CLI
5d69289 Cover saved runs, trace trimming, the CLI flags and the formatting module
1ade3a7 Show saved runs after a restart, trim traces, and give the CLI a wait limit
91b689c Merge stage-7-pipeline: early result, shop cards, per-shop retry, result v2
ea064a1 Cover the shown-then-updated result, shop statuses and the per-shop retry
f6cf7f0 Show a result early and update it per shop, with honest shop cards and retry statuses
26a65a6 Merge stage-6-matching: default pre-filter, match outcomes, verdict reuse, similar products
e97cb2d Test the Stage 6 filter caps, outcomes, recall safety and similar products
50d8866 Filter listings by default, add strict mode and ask for similar products
965d07f Give every match an outcome, reuse verdicts and let the scraper price win
4690a59 Merge stage-5-llm: persisted endpoint limits, gateway errors, prompt v2
af15c1e Test used-up LLM state, the two gateway errors and prompt v2
feb8050 Keep a run alive when the LLM is out of allowance and record the Stage 5 decisions
36c8a1b Persist used-up LLM endpoints, add gateway error classes, sharpen the prompts
39a6e2b Merge stage-4-search: parallel engines, engine health, Indian shop allow-list
8e2fd18 Raise the discovery time budget and record the Stage 4 decisions
b9844e8 Ask search engines in parallel, skip failing ones, allow-list Indian shops
15aa697 Merge stage-3-shops: shop status, light Flipkart, structured generic pages, GeM price ranges
f247f4c Record the Stage 3 decisions
2ad732b Turn the light Flipkart fast path on by default
bc3dda2 Report blocked and parser-empty shops, read JSON-LD, keep price ranges
576dcf2 Merge stage-2-foundations: shared keys, redaction, run index, trace age limit
8dd9498 Test shared listing keys, redaction, the run index and cache keys; align the scorer
2e765a2 Add shared listing keys, secret redaction, an on-disk run index and age-based trace rotation
83edb8a Merge stage-1-safety: generic scraper fix, honest fakes, network guard
e12f784 Fix the generic scraper crash and make the tests honest about the network
```

Final test count: **701 passed** (`python -m pytest -q`).

## 7. Confirmations

- **No secrets in any new commit.** A scan of every added line for `nvapi-`,
  `gsk_`, `AIza`, `sk-or-` finds only the *fake* keys used by
  `tests/test_redact.py` and `tests/test_runs_store.py`, plus the redaction
  patterns themselves.
- **`.env` and `llm_providers.json` were never read into a commit and are not
  in the history.** `git log --all -- .env llm_providers.json` is empty.
- **History was not rewritten.** Nothing was force-pushed and no commit was
  amended. Main was moved back once before its first push in stage 9, so the
  stage still got its own branch and merge commit.
- **Nothing was run live.** No real LLM endpoint, search engine or shop page
  was contacted while building or testing.

## 8. Your live check list

1. `python -m pytest` → all pass.
2. Copy the two example files: `.env.example` → `.env`, and
   `llm_providers.example.json` → `llm_providers.json`. Fill in the keys.
3. `python tools/doctor.py` → no `FAIL`.
4. `python tools/doctor.py --live` → LLM, search and shop canaries.
5. `python tools/probe_endpoints.py` → one line per endpoint.
6. `python cli.py "<a GeM link>"` → the first result, then `Updated:` lines.
7. `uvicorn api.server:app`, open http://127.0.0.1:8000, paste the same link →
   the cards fill in live.

If a step fails, send: the failing test names, the `FAIL` lines, the saved page
path under `debug_html/`, or the trace id.
