## Fast profile (switch the safe speed options on together)

The defaults keep the original, slower behaviour. To get the speed work, put these lines in `.env`
(or set them in the terminal before you start the app):

```
GEM_PARALLEL=1
GEM_LLM_WORKERS=4
GEM_PRICE_PREGATE=1
GEM_REUSE_VERDICTS=1
GEM_PREFILTER_TOP=8
```

| Switch | What it does | Risk | Advice |
|---|---|---|---|
| `GEM_PARALLEL=1` | Flipkart, Amazon and other shops are searched at the same time; each lane checks its own listings as they arrive. A crashing lane no longer kills the run. | Low | On |
| `GEM_LLM_WORKERS=4` | Checks 4 listings at the same time over all providers. | Provider limits (see `max_concurrent`, `tokens_per_minute`) | On, lower it if you see many 429 errors |
| `GEM_PRICE_PREGATE=1` | Skips the LLM for listings whose price is clearly outside the allowed band. | Low | On |
| `GEM_REUSE_VERDICTS=1` | On the retry, does not ask again about listings already judged. | Low | On |
| `GEM_PREFILTER_TOP=8` | Keeps the best 8 listings per shop (model code first, then brand, then word match) before the LLM. Listings with the model code may go beyond 8. Dropped listings are saved in the trace. | Medium: could cut a real match | Use only after the scoring fix; check `python -m eval.analyze_misses` |
| `GEM_FLIPKART_LIGHT=1` | Chrome skips images. | Flipkart may react differently | Measure first |
| `GEM_SLIM_REFERENCE=1` | Shorter product text in the check prompt. | Could change answers | Measure first |
| `GEM_DISCOVERY=0` | Skips other-shops discovery completely. | Loses non-Flipkart/Amazon shops | Only if the measurement says it finds almost nothing |
| `LLM_CACHE=1` | Remembers LLM answers on disk. | Use `LLM_CACHE_TAG` per model when comparing models | For replay experiments |

PowerShell: `$env:GEM_PARALLEL="1"` before `python ...`. Git Bash: `GEM_PARALLEL=1 python ...`.
