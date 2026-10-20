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
