# Round 7 report

Branch: polish-v3. Six commits on top of main. No changes were pushed to main.

## Per task

### Task 1: card facts

Version 3 of the highlights prompt adds shopper facts and a short summary.
The card shows up to 8 facts and 14 are kept internally. The low priority
client stays outside the router, has a 20 second cap, and never blocks the
result. The run summary logs `highlights: llm|fallback|none`. Demo mode uses
fake highlights and never calls a real LLM.

Side by side, the demo card now shows a bought-facts strip instead of a raw
spec dump. The 8 shown facts are Brand, Model, Connectivity, Colour, Sensor,
Buttons, Battery life and Warranty.

### Task 2: shop icons

Icons are requested as soon as a domain appears, using 4 daemon workers, a
30 day disk cache and a 1 hour in memory negative cache. The API route sends
a 7 day public cache header; a 204 reply is `no-store`. The page retries and
reprobes once at row finish and again at run end, and falls back to a globe.
The collector saved 40 icons; 9 domains failed and keep the globe.

### Task 3: price chart

The zero based bars are replaced by one shared strip. The GeM price is a
marked line, cheaper listings are green and dearer ones are red. The axis
pads by 8 percent, keeps a minimum span of 3 percent of the GeM price and
never goes negative. `priceScale` has unit tests, the chart has an accessible
label plus a hidden list, and the layout fits 360 px.

### Task 4: pipeline cells and honest time

Each cell now has one exact state: muted dash, running value with a pulse,
done value, dash with a "not needed" title, or dash with a reason when it
failed. The word waiting is gone from the pipeline, queue, LLM note, page
title and initial HTML. A finishes run has no running or not started cells.
Shop seconds come from that shop's own events, not from the shared lane
clock, and timed out is only used when a shop produced no verdict at all.
A printer trace replay test checks the whole pipeline DOM after done.

### Task 5: parallel proof

A fake lane test runs Flipkart at 3 scaled seconds, Amazon at 1, discovery
at 2 and four shop pages at 1 each, all in 0.30 s of test time. It proves all
three lane start events are within 100 ms, Amazon verification starts before
Flipkart search ends, and the four pages are read together rather than in
sequence. The trace now carries `stages.starts` with lane and shop offsets.
The System tab prints them as "Lane start offsets".

Live run `a71232510a164227` (Canon PIXMA G1730):

- lanes: Flipkart 8.949 s, Amazon 8.950 s, other shops 8.950 s
- shops: Flipkart 8.949 s, Amazon 8.950 s, JioMart 21.048 s, Cashify 21.049 s,
  IndiaMART 21.050 s, Computechstore 21.051 s, BigBasket 21.052 s,
  PrinterPoint 21.053 s

The three lanes start together; the other shops start later because discovery
has to find them first.

### Task 6: retry only what can change

A shop is retried only for a timeout, connection reset, server error, an
unfinished lane or a clean empty search with no match. A 403, 404, 410,
CAPTCHA, bot wall, robots disallow, DNS failure and document page rejection
are final. Once a match exists only an incomplete or timed out Flipkart or
Amazon lane can retry. The stage label now names the shops being retried.

Live Canon printer comparison:

- old run `bc9bec95d01d4b97`: 93.0 s total, 50.4 s after the first result,
  1 expansion call, retry for cashify.in and croma.com, 7 matches.
- new run `a71232510a164227`: 57.8 s total, 0 s after the first result,
  0 expansion calls, no retry, 7 matches.

The final match count is the same. Total LLM calls are not comparable
between the two live runs (12 and 17) because the listing pool changed; the
retry itself spent exactly 1 expansion call and 50.4 s in the old run. The
cheapest shop also moved from digitechshop.co.in to amazon.in at ₹8,999
because live prices changed.

### Task 7: proof

- Full suite: 1033 passed.
- Golden loop: 30 of 30 iterations passed.
- Ruff: clean.
- Doc gate: the only matches are the frozen prompt strings in
  `llm/client.py`.
- Browser tests: `tests/test_frontend_browser.py` passed; 12 new screenshots
  are in `docs/screens/` for start, mid run and done, in light and dark, at
  1440 and 390 px.

Six interleaved pairs, all with `--refresh --no-wait --json`:

| Pair | Product | Main | Branch | Delta |
| --- | --- | --- | --- | --- |
| 1 | Canon G1730 | 128.25 s | 110.24 s | -18.01 s |
| 2 | Epson L130 | 85.05 s | 66.44 s | -18.61 s |
| 3 | Canon G1730 | 129.63 s | 74.87 s | -54.76 s |
| 4 | Epson L130 | 70.28 s | 64.65 s | -5.63 s |
| 5 | Canon G1730 | 151.25 s | 65.06 s | -86.19 s |
| 6 | Epson L130 | 79.52 s | 124.30 s | +44.78 s |

Median main 106.65 s, median branch 70.66 s, median paired delta -18.31 s.
Cooldown events: main 6, branch 2. LLM failures: main 7, branch 2. The spread
of paired deltas is far larger than the median effect, so the result is not
distinguishable from noise. The branch median did not worsen.

## serve.py diagnosis

`scratch/serve-check.log` shows the doctor passed, uvicorn reached
"Application startup complete", and the app served `/api/health` with 200.
The flood of 404s is `GET /health`, a different path that this app has never
exposed. It comes from an external probe, not from the page. serve.py did
start; the wrong health URL made it look broken.

## Not done

- The Round 6 speed gate still fails on the old baseline.
- `docs/review/` is left untracked.
- `stash@{0}` from the other session is untouched.
- Owner approval items were not changed.

## Click through on a real run

1. Card: the 8 fact strip and the short summary.
2. Pipeline: exact cell states, pulse while running, no waiting text.
3. Price chart: shared zoomed axis, GeM line, green and red segments.
4. Shop rows: favicons and per-shop seconds that differ.
5. System tab: the "Lane start offsets" fact after a real run.
6. Activity: a retry, when one is really earned, names its shops.
