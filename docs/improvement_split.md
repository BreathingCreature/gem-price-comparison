# DEV / HELD-OUT split (written before any code change)

Date: 2026-10-22. Purpose: every improvement is measured on a DEV half while
fixing, and checked once on a HELD-OUT half that is not used to choose fixes.

The answer key holds 27 products (18 branded, 9 generic) and 54 wanted slots
(7 Amazon cells, 4 Flipkart cells, 43 other-shop URLs). See
`docs/evaluation.md` for the slot rules.

## Rule used

Sort the ids inside each group (branded, generic) and hold out every third
product (3rd, 6th, 9th, ...). That yields the requested 18 DEV / 9 HELD-OUT
and keeps both halves mixed:

* DEV: 12 branded + 6 generic (18 products, 35 slots)
* HELD-OUT: 6 branded + 3 generic (9 products, 19 slots)

The brief's parenthetical ("ids ending in an even position when sorted")
cannot produce 18/9: 27 sorted ids have 13 or 14 even positions. The counts
(18/9) and a mixed split were taken as the intent; the rule above is the
deterministic version of them. This file is written before the first code
change so the split cannot drift towards whatever the fixes happen to hit.

## DEV (18) - may be studied while fixing

P01 P03 P05 P08 P11 P21 P26 P28 P29 P36 P37 P38 P41 P42 P43 P45 P49 P50

* branded (12): P01 P03 P05 P08 P21 P26 P28 P36 P41 P42 P43 P45
* generic (6): P11 P29 P37 P38 P49 P50

## HELD-OUT (9) - numbers read once, at the end

P04 P10 P25 P30 P33 P39 P40 P51 P53

* branded (6): P04 P10 P25 P33 P39 P53
* generic (3): P30 P40 P51

## Baseline on this split (`fixed2`, strict)

| Half | Products | Slots | Found | Recall |
|---|---|---|---|---|
| DEV | 18 | 35 | 2 (P03 Flipkart, P42 Amazon) | 5.7% |
| HELD-OUT | 9 | 19 | 2 (P04 Flipkart, P51 other) | 10.5% |

Whole key, same run: 4/54 = 7.4% recall, 31.2% precision (5/16 predictions).

## Honesty note

Step 1 asks for the lost step of every listing, so its diagnosis was run over
all 27 traces before this split existed. From this file onwards, fixes are
chosen from DEV evidence only; the HELD-OUT traces are not opened again and
the HELD-OUT score is computed once, on the final run. Two fix candidates
were therefore rejected even though they looked promising, because their only
evidence was on HELD-OUT products (the Amazon card-title loss in P25 and the
IndiaMART prefilter drop in P10).
