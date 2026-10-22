# Why Amazon recall fell between the "before" run and fixed2

fixed2 found a right Amazon listing for 1 of 7 wanted Amazon slots (14.3%).
The "before" run found 3 of 7 (42.9%). The score files are
`eval/results/before.score.json` and `eval/results/fixed2.score.json`; the per
product rows are in `eval/results/fixed2.jsonl` (trace ids) and
`eval/results/before.jsonl`.

Two products carry the whole difference: **P03** (Epson EcoTank L130) and
**P33** (Mediguard oxygen hood). Both were opened in their traces and every
stage was compared. Neither loss comes from a scraper or pipeline bug, so no
code was changed.

## P03 - Epson EcoTank L130 (the listing was never in the results page)

Key: `B00WP39JLG` (product page) and `B0BBQDHM68` ("EcoTank L130 Single
Function InkTank Printer", Rs 10,099).

| | before `b63d5755729b4f9f` | fixed2 `4d1bd03b04264390` |
|---|---|---|
| query | `EPSON L130 A4 colour ink tank printer` | same |
| Amazon cards scraped | 15 | 15 |
| key card `B0BBQDHM68` scraped | yes, Rs 10,099 | **no** (not on the page) |
| verdict on it | same model L130, 0.95 -> match | not seen |
| GeM price used | 7,999 | 7,999 |

The fixed2 page was the same query but a different ranked list: 11 cards
overlap, 4 differ. The key card was replaced by more ink/cover listings, which
the model correctly rejected ("ink bottles, not printer"). The price gate also
rejected several ink bottles (Rs 274-987 against a 0.15x floor of Rs 1,200) -
the same thing happened in the before run, so that is not the change either.

`GEM_PREFILTER_TOP` does not explain it: the fixed2 run used 15 and the before
run used 10, and both runs scraped 15 cards before the filter.

## P33 - Mediguard oxygen hood (the verdict changed, the card did not)

Key: `B0BSRZJSL1` and `B0BSRZQTJS`, both Rs 1,890. GeM price Rs 1,092.

| | before `6ea62b2a547b4b2f` | fixed2 `99587321e7434306` |
|---|---|---|
| key cards scraped | both | both |
| card title served for `B0BSRZQTJS` | `MEDIGUARD Oxygen Hood Seamless Construction...` | `Oxygen Hood Seamless Construction...` (no brand) |
| verdict on `B0BSRZQTJS` | same model JESI-OH-001, 0.95 -> match | different model code, 0.9 -> possible |
| verdict on `B0BSRZJSL1` | model number missing, 0.9 | different price, 0.8 |

Same two ASINs, same prices, same reference product (model `JESI-OH-001`
extracted in both runs). What changed is the title text Amazon served for the
same URL, and the model's answer on top of it. The free-model chain also
changed between the runs: fixed2 ran while aion, openrouter and google were
near or past their daily quota, so most verify calls went to the NVIDIA models.

## What this means

- The two Amazon slots were lost at two different steps: P03 at **retrieval**
  (the key card was not in the ranked page), P33 at **verification** (the card
  was there and the model said no).
- The strict key wants exact ASINs; Amazon's ranked page and even the card
  title for one ASIN change between fetches, so the same code gives different
  Amazon recall on different days.
- No stage dropped a card the key names: prefilter drops in both runs are all
  different-model printers and ink bottles.

Candidate changes, not applied (they change behaviour, so they need a decision):

1. When a wanted key ASIN is known, ask Amazon for `/dp/<asin>` directly
   instead of relying on the ranked search page.
2. Treat the ASIN as the identity: if a scraped card URL is the key ASIN, mark
   it a match even when the served title is thin.
3. Keep the model out of the identity check: compare card titles against the
   reference with the existing prefilter score as a second gate when the model
   says "different model code" at high confidence on a card whose URL matches a
   key ASIN.
