# Evaluating the project

This is how to measure how good the project is, with numbers you can put in a report or a paper. Everything here runs from the project folder.

The idea: you look up a set of GeM products **by hand** on the shops and write down the right answer (the *answer key*). The pipeline runs on the same products. A script compares the two. The answer comes from you, not from Google or from the pipeline itself, so the test is fair.

## 1. The answer key

One row per GeM product, in `eval/answer_key.csv`. Start from `eval/answer_key_template.csv` (copy it, keep the header line).

| Column | What to write |
|---|---|
| `id` | A short unique name: P01, P02, ... |
| `gem_url` | The GeM product page link |
| `gem_name` | The product name, for your own reading |
| `branded` | `yes` if it has a brand and model number (a Logitech M235 mouse), `no` if it is generic (a steel almirah). Branded items are far easier, so they are scored separately. |
| `gem_price` | The GeM price, for your own reading |
| `amazon_url` | See below |
| `amazon_price` | The selling price on that page, a plain number like `1299` |
| `flipkart_url`, `flipkart_price` | Same for Flipkart |
| `other_urls` | Listings on any other shop (Croma, Reliance Digital, the brand's own shop). Several links separated by a space. |
| `notes` | Anything worth remembering |

For each shop column there are three kinds of answer:

- **Empty**: you did not look at that shop for this product. It is ignored.
- **`NONE`**: you looked properly and the product is not sold there.
- **A link**: this is the same product. If two listings are both right (two sellers of the same item), write both links with a space between them. Finding either one counts.

"Looked properly" means: search the shop for the brand and model, then for the product name, and check the first page of results. Same product means same brand, same model and the same size or capacity. A different colour is fine.

Example row:

```
P01,https://mkp.gem.gov.in/....,Logitech M235 Wireless Mouse,yes,949,https://www.amazon.in/dp/B01234ABCD,899,NONE,,,found by model number
```

Check the file at any time:

```
python -m eval.score --check-key eval/answer_key.csv
```

It lists every problem (a link in the wrong column, a repeated id, an empty row) and says "0 problem(s)" when the file is good.

**How many products:** 30 to 40, with a mix of about half branded and half generic. About an hour of work for every five products. Fewer than 30 gives numbers that jump around too much to trust. Keep a list of the products you gave up on, and say how many in the report.

## 2. Running the pipeline on the key

```
python -m eval.run_eval --key eval/answer_key.csv --label before --limit 3
```

Run three first and look at the output. Then run all of them (the same command without `--limit`). It saves `eval/results/before.jsonl` (one line per product) and `eval/results/before.meta.json` (the settings and the git commit). You can stop it with Ctrl+C and start it again later: finished products are skipped.

**Restart behavior:** On a resume, any row whose result line has no `error` is treated as done and skipped. A row where the LLM never answered (all LLM calls failed, no cache hits) counts as an error and is run again. A row with matches but some failed LLM calls is also treated as done — those failures were retried internally by the router.

If you reuse a `--label` with different settings (for example `GEM_PARALLEL=1` vs `0`), the old `.meta.json` is kept and a warning lists the settings that changed. Use a new `--label` for a new setting.

Every product is a fresh run. The one-hour cache of finished runs is bypassed.

## 3. The score

```
python -m eval.score --key eval/answer_key.csv --results eval/results/before.jsonl --json eval/results/before.score.json
```

What the numbers mean:

- **Recall**: of the listings you found by hand, how many did the pipeline also find. This is "coverage". Per shop too.
- **Precision**: of the matches the pipeline reported, how many were right.
- **F1**: one number that mixes the two (high only when both are high).
- **Price within 2%**: for found listings, how often the price the pipeline reported is within 2% of the price you wrote down.
- **Median s and p90 s**: time for one product. Half of the products are faster than the median, and nine in ten are faster than the p90.
- **Errors**: products where the run crashed.

Only shops you checked count. A shop you left empty is never counted for or against the pipeline.

## 4. The baseline without the LLM

To say "the LLM helps", compare with something simple. This one reuses the listings the pipeline collected and picks by title similarity alone (the best listing per shop if at least half of the words agree):

```
python -m eval.baseline_fuzzy --results eval/results/before.jsonl --out eval/results/fuzzy.jsonl
python -m eval.score --key eval/answer_key.csv --results eval/results/fuzzy.jsonl
```

Add `--price-band` to also drop listings priced outside 0.3 to 3 times the GeM price. It needs the trace files in `traces/` from the run, so run it before cleaning that folder.

## 5. The settings (switches)

| Setting | Normal | Effect |
|---|---|---|
| `GEM_PARALLEL` | `0` | `1` searches Flipkart and Amazon at the same time |
| `GEM_RETRY` | `1` | `0` skips the second try with new search words |
| `GEM_GATES` | `1` | `0` switches off the currency check and the price-band check |
| `PRICE_SANITY_LOW_RATIO`, `PRICE_SANITY_HIGH_RATIO` | `0.15`, `5.0` | The allowed price band, as a multiple of the GeM price |
| `MATCH_CONFIDENCE_THRESHOLD` | `0.75` | The least confidence the LLM needs to give |
| `LLM_CACHE` | `0` | `1` remembers LLM answers on disk, see below |
| `LLM_CACHE_DIR` | `cache/llm` | Where the remembered answers live |
| `LLM_PROVIDERS_FILE` | `llm_providers.json` | Which LLM endpoints are used |
| `GEM_LLM_WORKERS` | `1` | How many listings are checked at once. Above 1 it uses the providers in parallel — keep each provider polite with `max_concurrent` and `daily_limit` in `llm_providers.json` (see docs/llm-providers.md) |
| `GEM_PREFILTER_TOP` | `0` | Keep only the best N titled listings per shop before the LLM. `0` is off; the safe N comes from section 9 |
| `GEM_PRICE_PREGATE` | `0` | `1` drops listings whose card price is already outside the band before the LLM call |
| `GEM_REUSE_VERDICTS` | `0` | `1` keeps the first pass's verdicts on the retry instead of asking the LLM again |
| `GEM_DISCOVERY` | `1` | `0` skips the other-shops discovery and the brand-site fetches |
| `GEM_FLIPKART_LIGHT` | `0` | `1` makes Flipkart's browser skip images and stop loading early. For timing, not for accuracy runs |
| `GEM_DISCOVERY_TIME_LIMIT` | `20.0` | Seconds the whole search-engine stage may take before giving up on the slow ones |

Set them for one run only.

- Windows PowerShell: `$env:GEM_PARALLEL="1"; python -m eval.run_eval --key eval/answer_key.csv --label parallel-on`, and afterwards `Remove-Item Env:GEM_PARALLEL`.
- Git Bash, Linux, macOS: `GEM_PARALLEL=1 python -m eval.run_eval --key eval/answer_key.csv --label parallel-on`

The settings used are written into the `.meta.json` of every run. If you set a variable in your shell and forget it, the next run still uses it, so check that file.

## 6. The experiments

Each experiment is one run with its own `--label`, then one `eval.score` command. Do them in this order. Run the accuracy experiments from a recording (section 7) so day-to-day shop changes cannot skew the comparison; the two timing runs (`before`, `parallel-on`) must stay live.

| Label | Settings | Answers the question |
|---|---|---|
| `before` | all normal | How good is the project as it is |
| `parallel-on` | `GEM_PARALLEL=1`, cache off | How much faster, and does the answer stay the same |
| `no-retry` | `GEM_RETRY=0` | Does the second try help |
| `no-gates` | `GEM_GATES=0` | Do the currency and price checks help |
| `single-llm` and `multi-llm` | `LLM_PROVIDERS_FILE` pointing at a file with one endpoint, then at one with several, and a separate `LLM_CACHE_DIR` for each | Does the router change quality |

### The LLM call budget

One product takes about 20 to 30 LLM calls. Forty products is about 1,000 calls for one full run. The free plans allow roughly 1,000 calls a day per provider. Plan for it:

- Use `LLM_CACHE=1` for `no-retry` and `no-gates`. Those switches change what happens **after** the LLM answered, so the saved answers can be reused and the run costs almost nothing. A run still uses the live shops, so the listings change a little between runs. Differences of one or two products are noise. Better: replay a recording (section 7), which removes the shop noise entirely.
- **Never** use the cache for timing runs (`before` and `parallel-on`). A saved answer is instant, so the time would be fake.
- **Never** share a cache folder between different models. The saved answer does not record which model gave it, so `single-llm` and `multi-llm` would silently reuse each other's answers. Give each its own `LLM_CACHE_DIR`.
- Time and accuracy for the headline numbers: run `before` twice. The difference between the two runs shows how noisy the shops are. Quote both.
- A full run is a day of one provider's allowance. With two or three providers in `llm_providers.json` the router spreads the load.

### Which model answered

The runs record which model answered each call (`llm_models` in the results file). With several endpoints the models differ, which makes a result harder to repeat. For the headline `before` run, use **one** model and say which in the report.

## 7. Comparing settings on the same listings (record and replay)

The shops return slightly different results every day, so two live runs differ for reasons that have nothing to do with the switch you are testing. Record the shops once, then replay those exact answers for every accuracy experiment:

```
python -m eval.replay --key eval/answer_key.csv --record baseline
python -m eval.replay --key eval/answer_key.csv --replay baseline --label parallel-on
```

The `--record` run is a live run (like section 2) that also saves every shop answer under `eval/recordings/baseline/`, one file per product. The `--replay` run never touches a shop: the same questions are answered from the recording, so only the switch under test changes.

- `--record NAME` writes the usual results file (`NAME.jsonl`). `--replay NAME` writes `NAME-replay.jsonl` unless `--label` says otherwise.
- Use `LLM_CACHE=1` for replays of the switches that only change what happens **after** the LLM answered (`GEM_PREFILTER_TOP`, `GEM_PRICE_PREGATE`, `GEM_REUSE_VERDICTS`, `GEM_GATES`): the saved answers are reused and the replay costs almost nothing. The model and router comparisons must not use the cache.
- Do **not** replay to compare the speed of shop-facing switches (`GEM_PARALLEL`, `GEM_FLIPKART_LIGHT`): a replay answers instantly, so the time would be fake. Take accuracy from a replay, time from a live run.
- If a replay stops with "replay divergence", the LLM produced a different search query than during the recording, so that product would have compared different listings. Use the same `LLM_CACHE_DIR` as the record run so the same query comes back, or record again.

## 8. Where the time and the LLM calls went (profiler)

```
python tools/profile_runs.py --results eval/results/baseline.jsonl
python tools/profile_runs.py --last 5
```

Every run leaves a trace in `traces/`. The profiler adds up, per run and in total: the wall time of each step (extract, normalize, the searches, discovery, match, retry), how many LLM calls happened inside the step, and tables of calls, failures and seconds per provider and per purpose. LLM seconds are exact even when steps run in parallel; with `GEM_PARALLEL=1` the step wall times overlap, so their sum can be larger than the run's total.

## 9. Why a match was missed, and what prefilter N is safe

```
python -m eval.analyze_misses --key eval/answer_key.csv --results eval/results/baseline.jsonl
```

For every listing the key says is real but the results file does not report, this walks the run's trace and names the reason:

- **never found**: no search ever returned the listing
- **fetch failed**: it was returned, but its page would not load
- **cut by filter**: the prefilter dropped it before the LLM saw it
- **LLM said no**: the model judged it a different product
- **safety gate**: the currency or price-band check hard-rejected it
- **below confidence threshold**, **matched but no price**, **verify incomplete**: the verdict was not good enough to confirm

When the reason is "cut by filter", the listing has a shop rank R and would have been kept whenever `GEM_PREFILTER_TOP` was at least R. The last table counts, for N = 5, 10, 15, 20, 30, 50, how many of the missed matches would have been put back in front of the LLM. Pick the smallest N that recovers what matters, then pay for it in `verify` calls: one extra call per recovered listing.

### The prefilter experiment

| Label | Settings | Answers the question |
|---|---|---|
| `prefilter-off` | `GEM_PREFILTER_TOP=0` | What does the prefilter cost in quality |
| `prefilter-10` | `GEM_PREFILTER_TOP=10` | The middle ground the safe-N table suggests |

Record once, replay both, score with `eval.score`, and read the LLM call counts from `tools/profile_runs.py`.

## 10. What to say in the report

- How many products, how many branded, how you made the key, and how many you dropped.
- That the key was made by hand by the author (a single person), which is a limit.
- The date of the run: shops change daily.
- That GeM and Amazon discourage automated fetching, the sample is small and the use is research only.
- The recall and precision from `eval.score`, the baseline result, and the time with and without parallel search.
