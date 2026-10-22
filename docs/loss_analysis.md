# Loss analysis (Step 1, no code changes)

Source run: `eval/results/fixed2.jsonl` - 27 products, 0 errors, trace ids per
product below. Result: 4 of 54 wanted slots found (7.4% strict recall),
5 of 16 predictions right (31.2% precision), Amazon 1/7, Flipkart 2/4,
other shops 1/43. The run was made on the `ade2871` tree; the traces are
still valid data (see `AGENTS.md`).

Method: a wanted slot is the scorer's own unit - one cell for Amazon, one for
Flipkart (any URL in the cell counts), one slot per other-shop URL. For each
wanted slot the trace was walked in this order: final matches -> matcher
verdicts -> prefilter drops -> scraped candidates -> discovery raw/kept URLs
-> shop status. URLs are compared by listing key and host+path, because the
trace writer redacts long alphanumeric runs (including some URL paths) and
search query values.

## Counts per shop (54 slots)

| Shop | Wanted | Found | Lost at |
|---|---|---|---|
| Amazon | 7 | 1 | 3 retrieval, 2 prefilter, 1 verification |
| Flipkart | 4 | 2 | 2 retrieval |
| Other shops | 43 | 1 | 18 host never returned by discovery, 11 key page never returned, 6 possible, 4 LLM rejection, 3 filter drop |

Reason buckets (a slot is counted once, at the earliest stage that lost it):

| Shop | Reason | n |
|---|---|---|
| other | host_not_reached (search never returned that host) | 18 |
| other | not_in_results (host reached, key page not returned/scraped) | 11 |
| other | possible (LLM judged same product, gate kept it unconfirmed) | 6 |
| other | llm_no (LLM rejected the listing) | 4 |
| amazon | not_in_results | 3 |
| amazon | filtered_prefilter (cheap filter dropped the key card) | 2 |
| flipkart | not_in_results | 2 |
| amazon | llm_no (verified, rejected) | 1 |
| other | filtered_prefilter | 1 |
| other | discovered_filtered (kept out of the scrape list by the URL filter) | 1 |
| other | discovered_not_scraped (fell outside the 8-page scrape budget) | 1 |
| other | found | 1 |
| amazon | found | 1 |
| flipkart | found | 2 |

## Per product

`A` = Amazon cell, `F` = Flipkart cell, `O` = one other-shop URL.

| id | B/g | trace | Amazon | Flipkart | Other shops |
|---|---|---|---|---|---|
| P01 | B | 0b116734970c43bb | - | - | host_not_reached [dell.com] |
| P03 | B | 4d1bd03b04264390 | not_in_results (ASIN absent from Amazon results) | found | host_not_reached [croma.com] |
| P04 | B | 6b6874df1f504b82 | not_in_results | found | host_not_reached [croma.com]; host_not_reached [vijaysales.com] |
| P05 | B | 19d60ad093634aaf | - | - | host_not_reached [industrybuying.com] |
| P08 | B | 12d04a1916be44e9 | - | - | host_not_reached [vashiisl.com] |
| P10 | B | ad5d3f8ffdae40bf | - | - | filtered_prefilter [indiamart.com] score 0.227 |
| P11 | g | fea9b41cb32d4d33 | - | not_in_results | host_not_reached [croma.com]; host_not_reached [poorvika.com] |
| P21 | B | 4e43cbad31a14408 | - | - | host_not_reached [zoho.com] |
| P25 | B | e533686dd494483c | filtered_prefilter score 0.364 | - | - |
| P26 | B | 199342fdc8144c2d | not_in_results | - | - |
| P28 | B | fda9c3ad79194dac | - | - | possible (price unknown) [satol.net]; discovered_not_scraped [industrybuying.com]; not_in_results [indiamart.com] |
| P29 | g | 523a405f86574368 | - | - | llm_no (hard-rejected: currency USD) [ambetronics.com]; not_in_results [indiamart.com] |
| P30 | g | 24a7eb8f500f4329 | - | - | llm_no (different model/specs) [medikabazaar.com]; discovered_filtered [quali55careshop.com]; llm_no (different model code and pack) [indiamart.com] |
| P33 | B | 99587321e7434306 | llm_no (different price) | - | not_in_results [shopmediguard.com] |
| P36 | B | dcaaba81b8004e06 | filtered_prefilter score 0.034 | - | llm_no (different model code) [shopmediguard.com] |
| P37 | g | 791e4fd44b7f48c1 | - | - | host_not_reached [jomedhealthcare.com]; host_not_reached [thehealthpoint.in] |
| P38 | g | b5558e9f660342e7 | - | - | not_in_results [indiamart.com]; not_in_results [indiamart.com]; host_not_reached [molsieve.in] |
| P39 | B | 1485ec8c1cc74190 | - | - | not_in_results [indiamart.com]; not_in_results [dial4trade.com] |
| P40 | g | ed745b187892438c | - | - | host_not_reached [equipmedy.com]; not_in_results [indiamart.com] |
| P41 | B | c00baeda5476464a | - | - | possible (price unknown) [nainasemi.com]; possible (price unknown) [tradeindia.com] |
| P42 | B | d4ee7861735e4a17 | found | not_in_results | - |
| P43 | B | e117ed465ef249ab | - | - | possible (price unknown) [merillife.com]; possible (different pack) [rajbiosis.in] |
| P45 | B | a0c6579920e64138 | - | - | possible (price unknown) [primegodrej.com]; host_not_reached [exportersindia.com] |
| P49 | g | e3326ac515b94b1d | - | - | host_not_reached [balajiindustries.in]; host_not_reached [waterjelblanket.com] |
| P50 | g | 5a0c21484d2d4bed | - | - | not_in_results [lifeguardindia.in]; not_in_results [firesafetydealer.com] |
| P51 | g | 1c02332b1ad749c0 | - | - | found [firesupplies.in]; host_not_reached [industrybuying.com] |
| P53 | B | d0aeb1b40be14967 | - | - | not_in_results [udyogisafety.com]; host_not_reached [kioskengg.com] |

## What each bucket actually is

### 1. The discovery backend was degraded for most of the run (biggest single effect)

`fixed2`: 80 discovery queries, 815 raw URLs, 146 kept URLs. The backend
health lines in every trace read:

* `brave: skipped after repeated failures` in 71 of 80 queries (the
  three-strike breaker trips at `ENGINE_FAILURE_LIMIT = 3`, then the engine
  is skipped for `ENGINE_SKIP_SECONDS`; every later query inherits it)
* `duckduckgo: ddg on cooldown for ~3500s` in 79 of 80 queries (a CAPTCHA
  earlier in the day set the 1-hour cooldown)
* `brave: RuntimeError: HTTP 429 (rate limited)` 9 times - the fetcher
  already rotates safari180 -> chrome150 -> chrome142, so all three
  fingerprints were rate-limited; that is a real limit, not a code bug.

Consequence: 7 of 27 products got **zero** usable other-shop URLs
(P01, P04, P08, P11, P21, P37, P49), and most of the rest ran on SearXNG
(whose instances have been returning empty shells). The `before` run was
worse: 34 kept URLs and 23 of 27 products with none. The other-shop loss
table is therefore a lower bound for a healthy-backend run, not a pure
code signal. No polite code change fixes a rate limit, so this is reported
as an environment effect, not "fixed" in Step 2.

### 2. Other shops: the key host was never returned by search (18)

Examples: dell.com (P01), croma.com (P03, P04, P11), industrybuying.com
(P05, P51), vashiisl.com (P08), zoho.com (P21), jomedhealthcare.com and
thehealthpoint.in (P37), molsieve.in (P38), equipmedy.com (P40),
exportersindia.com (P45), balajiindustries.in and waterjelblanket.com
(P49), kioskengg.com (P53). Most are brand stores or small B2B shops;
with Brave rate-limited and DDG cooling down, the remaining backend did not
surface them. These are search-coverage losses, not filter or LLM losses.

### 3. Other shops: host reached, key page not returned (11)

The search engine returned a *category/company* page on the right host, not
the product page. Examples: P39 dial4trade returned
`/bgl-cylinders-industries/products.htm` instead of the BGL B-type cylinder
page; P38 IndiaMART returned a Newttorr company page; P53 udyogisafety
returned `/head-protection/helmet` instead of `/products/lighton`;
P28 IndiaMART returned a different Satol listing id. Raising the scrape
budget or re-scraping the same host's category pages would not conjure the
product page; this is a query/engine recall problem.

### 4. Other shops: possible, not confirmed (6)

| Product | Key URL? | Confidence | LLM reason | Why it stayed possible |
|---|---|---|---|---|
| P28 satol.net | yes | 0.85 | Same product, pack size not stated | price unknown on the page |
| P41 nainasemi.com | yes | 0.90 | same model code | price unknown |
| P41 tradeindia.com | yes | 0.85 | same model code | price unknown |
| P43 merillife.com | yes | 0.75 | same product name | price unknown |
| P43 rajbiosis.in | yes | 0.20 | different pack | weak + different pack |
| P45 primegodrej.com | yes | 0.80 | same model | price unknown |

`llm/match.py::_outcome` turns `is_match and price is None` into "possible"
unconditionally. Five of these six are key URLs with confident same-model
evidence; the sixth (0.20) is correctly left alone. This is the largest
*safe* recall lever visible on DEV: +5 slots, all of them predictions the
key already calls right.

### 5. Other shops: LLM said no (4)

P29 ambetronics.com was hard-rejected for a USD price (the currency gate
working as designed; the key's price for that row was transcribed in USD);
P30 medikabazaar.com and IndiaMART were rejected as different
model/specs/pack; P36 shopmediguard.com was rejected on model code. These
are honest rejections, not losses to chase.

### 6. Amazon: retrieval (3) and prefilter (2)

* P03: both key ASINs are absent from the Amazon search cards for the one
  query that ran (the lane stopped after its first query hit
  `DIRECT_SEARCH_MIN_CANDIDATES = 5` relevant cards). The open-web
  discovery *did* return `amazon.in/dp/b00wp39jlg`, but the other-shop lane
  drops Amazon/Flipkart links by design, so it was never checked.
* P04: the key ASIN is absent from 60 scraped cards across three queries.
* P26: the key ISBN/ASIN is absent from the cards; the query set is
  "Philip Larkin selected poems", "Lakshmi Narain Agarwal ...", and an ISBN
  for the Indian edition - the key ASIN is the Faber edition.
* P25 (prefilter 0.364): the card title for the key ASIN lost its brand
  ("Copier Paper - A4, 75 GSM..." instead of "JK Copier Paper ..."). The
  same ASIN was scraped again with the brand title, but the drop decision
  used the first card.
* P36 (prefilter 0.034): the card title has neither brand nor model
  ("Oxygen Flowmeters For Cylinders and Hospitals (FA VALVE ...)").

### 7. Flipkart: retrieval (2)

P11 and P42: the key `pid` never appears in the Flipkart cards for the one
query that ran per lane. P11's *retry* expansion did find the exact key URL
(croma.com) but the LLM rejected it on model code (UA75U8000FUXXL vs GeM
HG75U800FAUXXL) - a verification decision, not a retrieval one.

## Caveats about the key itself (affects what "found" means)

Four listings were reached on a *different URL than the key records*, so
strict scoring does not credit them even though the run did confirm a page
for the same product/shop:

* P50 firesafetydealer.com: run matched `/ahmedabad/products/lifeguard-4kg...`
  while the key has `/pune/products/...` (same listing, city variant).
* P33 shopmediguard.com: the run found the M and L oxygen hoods; the key has
  the S size (`...-s-seamless-construction`).
* P40 IndiaMART: the run found two HekaFlo HM-P-500 listings; the key has a
  different IndiaMART listing id.
* P39 dial4trade.com: pages on the right host were scraped, not the exact
  product URL.

Conversely, confirmed matches on several shops are *different listings of
the same product* (P04 has 4 Flipkart matches, P25 3 Flipkart, P26 4
Amazon/Flipkart editions, P40 2 IndiaMART). The scorer counts every one of
them as a separate prediction and only the key URL as correct, which is why
strict precision (5/16) is much lower than a human reading the same list
would say. Hence the human-check sheet in Step 4.

## Levers, ranked by DEV-only evidence

| # | Lever | DEV slots reachable | Evidence |
|---|---|---|---|
| 1 | Confirm a "possible" listing when it is the same model (price may stay unknown) | +5 | P28, P41 x2, P43, P45 (all DEV) |
| 2 | Route open-web Amazon/Flipkart product URLs into their own lane instead of dropping them | +1 proven | P03 (DEV): discovery returned the exact ASIN the Amazon lane missed |
| 3 | Run the unused normalized queries for a direct lane that confirmed nothing | +0-2 | P03, P26 (DEV): one query ran, two were skipped by the early exit |
| 4 | Prefilter safety net (send the next-best dropped cards when a shop saw few) | +0-1 | P36 (DEV) |
| 5 | Raise the other-shop scrape budget past 8 | +1 | P28 industrybuying (DEV), but it is one page and costs politeness |
| 6 | Discovery backend health | many, environment | see section 1; no polite code fix |

Fixes for Step 2 are chosen from this table on DEV only: lever 1 first, then
lever 2 (proven, no extra search request; lever 3 is the fallback if lever 2
cannot be made safe).
