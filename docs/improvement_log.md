# Improvement log — Step 2 (two recall fixes + one honesty fix, measured honestly)

Date: 2026-10-22. Baseline for every number below: `eval/results/fixed2.jsonl`
(27 products, 54 slots, recorded on the `ade2871` tree before the revert; the
traces are still valid data). The DEV/HELD-OUT split was written in
`docs/improvement_split.md` before any code change; fixes were chosen from DEV
evidence only.

## What changed

### Fix 1 — a same-product verdict with no price can be a confirmed match
- `core/prefilter.py::identity_evidence(title, page_text, product)` — a gate,
  not a ranking: the model code (spaces/dashes ignored) appears in the title or
  on the page, or the brand plus at least two of the product's own words appear
  in the title. A title with neither is not evidence.
- `llm/match.py::_identity_proof` + `_outcome(...)` — a listing the LLM called
  `is_match` at confidence >= 0.75 with **no price** is now confirmed when code
  proves the identity. The price stays `None` (unknown is never guessed).
  `different pack`, hard-rejected rows and confident "no" verdicts never
  promote. Confirmed matches with unknown price sort last (no crash, no
  "unknown = free").
- Why: brand stores and B2B shops ask for a quote, so the page carries no
  number; the product was real but sat in "possible" forever.

### Fix 2 — one extra query per direct lane before calling the shop empty
- `core/pipeline.py::_run_first_pass_lanes().direct_lane` — when a lane
  confirmed nothing and `search_direct`'s early exit left a query unrun, the
  lane searches **one** more normalized query, verifies the fresh URLs (deduped
  against the first pass) and merges the verdicts. Never after a confirmed
  match, never a loop, never the same URL twice.
- Why: the early exit stops at 5 relevant cards; P03's trace shows both key
  ASINs were absent from the one query that ran while discovery had seen the
  product URL.

### Fix 3 — a failed web search is "not searched", never "not found"
- `core/pipeline.py::_shop_entries` — when every discovery query ended with a
  backend failure (DDG on cooldown, Brave 429, "no result links could be
  parsed") and no URL came back, the other-shops card now reads
  `not_searched` ("the web search failed, so other shops were not searched")
  instead of `not_found` ("searched, nothing confirmed"). A clean empty search
  still reads `not_found`.
- Found by the live run below: P28's card claimed it had searched while
  `seen` was 0 and `search_issues` said all backends failed.

## Tests (fails on old code, passes now)
- `tests/test_possible_confirmed.py` (10): model in title, model on page,
  brand + 2 words, one-word title stays possible, plain title stays possible,
  different pack never promotes, confident "no" never promotes, unknown price
  sorts last, evidence helper (dash/space, brand-only rejection).
- `tests/test_lane_query_retry.py` (5): retry keeps the second query's match,
  a confirmed first query spends no second search, single query = nothing to
  retry, a blocked search is not retried, a shared URL is verified once.
- `tests/test_pipeline_stage7.py` (+2): a failed web search is not_searched
  and retryable; a clean empty search is still not_found.
- Proof: `git stash push core/prefilter.py llm/match.py core/pipeline.py` →
  8 of the new tests fail (the rest guard behaviour that must not change);
  restore → all pass.
- Two old `tests/test_pipeline.py` assertions encoded the pre-retry call
  sequence; they now assert both stages (early exit, then exactly one retry).

## Side-by-side check (same saved fixed2 traces, new decision logic replayed)

Strict scoring (only answer-key URLs count):

| set | slots found | recall | predictions | correct | precision | other shops (products) |
|---|---|---|---|---|---|---|
| ALL before | 4/54 | 7.4% | 16 | 5 | 31.3% | 1/24 |
| ALL after | **9/54** | **16.7%** | 29 | 10 | **34.5%** | **5/24** |
| DEV before | 2/35 | 5.7% | 7 | 3 | 42.9% | 0/16 |
| DEV after | **7/35** | **20.0%** | 18 | 8 | **44.4%** | **4/16** |
| HELD before | 2/19 | 10.5% | 9 | 2 | 22.2% | 1/8 |
| HELD after | 2/19 | 10.5% | 11 | 2 | 18.2% | 1/8 |

With-possible (every listing the run already *saw*, confirmed or not):
ALL 10/54 = 18.5% before and after. So Fix 1 found nothing new — it moved 9
already-visible listings from "possible" to "confirmed" and raised precision
while doing it.

Flip list (13 rows; 5 are answer-key URLs → the +5 strict slots):
P03 epson.co.in, P05 moglix, P28 satol.net*, P28 bluepointoffice,
P28 indiamart, P29 indiamart, P41 nainasemi*, P41 tradeindia*,
P43 merillife*, P45 primegodrej*, P50 indiamart; HELD: P39 dial4trade ×2.

## Live whole-key run (`final_fix1_fix2`, new code, 2 workers) — environment-degraded
- 27/27 products, 0 errors, 236 LLM calls (11 failed attempts), median 40.6 s,
  p90 62.0 s. Settings: `eval/run_eval.py --key eval/answer_key.csv --label
  final_fix1_fix2 --workers 2`.
- Strict: **4/54 (7.4%), precision 26.3% (5/19)**; Amazon 28.6%, Flipkart
  50.0%, other shops 0/24. With-possible: 4/54 (12.2% precision).
- **The web search was down for most of the run**: DuckDuckGo was on a 1-hour
  cooldown from an earlier run (78 queries) and Brave answered HTTP 429 then
  went on its cooldown (64 queries). 20 of 27 products got **zero** other-shop
  URLs, so the pages Fix 1 is about were never fetched. The other-shops lane is
  therefore not measured by this run.
- Fix 1 did fire, 4 times, all on P03 (epson.co.in product page + catalogue
  page, shyamtrading.in, myg.in — all the exact L130, price unknown). None is
  an answer-key URL (P03's key is the Flipkart listing), so predictions rose
  and strict precision fell from the fixed2 31.3% to 26.3%.
- Conclusion: the live run is an honesty check, not a measurement of the fix.
  The replay above is the controlled before/after (same saved pages, only the
  decision rule differs). A re-run on a network where the search backends work
  is still needed to confirm Fix 2 and the other-shop gains.
- The run also exposed Fix 3 (P28's card claimed "searched" with 0 listings).

## Caveats (read these before quoting a number)
- **HELD-OUT precision fell 22.2% → 18.2%** although recall held. Both P39
  flips are the manufacturer's catalogue pages on `dial4trade.com`
  (`/bgl-cylinders-industries/products.htm` and the seller root); the answer
  key lists that seller's *product* page, so strict scoring counts two extra
  predictions. The pages are the same product family, not a different product;
  the strict key cannot express that. The rule was not changed afterwards —
  that would be tuning on the held-out half.
- 8 of the 13 confirmations are real same-product pages on shops the key does
  not list (resellers, catalogue pages). They are why predictions rose from 16
  to 29 and precision still rose; they are also why "precision" here is a
  lower bound on correctness.
- Fix 2 cannot be measured offline: whether the unused query surfaces P03's
  ASIN depends on the live search. Its unit tests prove the behaviour (one
  extra search, only when empty, deduped); its live effect is unproven.
- The fixed2 traces were produced by the reverted `ade2871` tree. The replay
  applies only the current decision logic to those saved verdicts; a fresh run
  produces its own verdicts.
- Thresholds unchanged: confidence 0.75, currency INR, 0.15x–5x price band,
  `PRICE_PREGATE` on, scraper price wins, unknown stays unknown.

## Settings used for verification
- Suite: `python -m pytest -q` → **784 passed** (767 before + 17 new).
- `python tools/doctor.py` → 7/7 pass (imports, 10 endpoints, cache/traces,
  frontend, demo run 63 events / 2 matches).
- Live whole-key run: `python eval/run_eval.py --key eval/answer_key.csv
  --label final_fix1_fix2 --workers 2` (one run this task, new code).
- Human-check sheet: `docs/human_check_sheet.md` (the run's 21 confirmed
  matches + the 13 replay flips, each with a blank "same product?" column).
  Regenerate with `python tools/human_check.py <results.jsonl> --out ...`.
