# Why 19 of 27 products ended with a shop on "possible"

A "possible" row is a listing the model judged to be the same product family
but the gate would not confirm: price missing, pack/quantity different, or
confidence below 0.75. The rows are in `final_result.possible_matches` in each
trace and (from this patch on) in the `possible` list of each result row.

Counted from the fixed2 run (`eval/results/fixed2.jsonl`, 27 products):
**19 products have at least one possible row, 52 rows in total**, spread over 24
shops. The reason the gate gave for each row:

| reason in `why` | rows | products |
|---|---|---|
| `different pack` | 30 | P25 (6), P42 (9), P50 (3), P51 (2), P43 (3), P01, P10, P49 |
| `price unknown` | 18 | P03, P05, P26, P28 (4), P29, P39 (2), P41 (2), P42 (2), P43, P45, P50 (2) |
| `confidence 0.50 below 0.75` | 2 | P33, P50 |
| `confidence 0.60 below 0.75` | 1 | P36 |
| `confidence 0.70 below 0.75` | 1 | P53 |

Per product (rows / shops): P03 1/1, P01 1/1, P05 1/1, P10 1/1, P25 6/3,
P26 1/1, P28 7/6, P29 1/1, P33 1/1, P36 1/1, P39 3/2, P41 2/2, P42 11/3,
P43 4/4, P45 1/1, P50 6/5, P51 2/2, P53 1/1, P49 1/1. The other 8 products
(P04, P08, P11, P21, P30, P38, P37, P40) have no possible rows at all.

The same 19 products are the ones whose every searched shop came back
`possible` or `not_found` (no `found`), except P42, where Amazon was found but
the other shops still had possibles.

## The dominant reason is pack size, not a weak model

30 of the 52 rows are `different pack`. The verify prompt has one explicit
rule: "A different pack size or capacity is a no - say no with the reason
'different pack'.", and `llm/match.py` maps that reason to `possible`. The
products affected are exactly the ones where the wanted unit is ambiguous:
A4 paper (ream vs box of 5), antigen test kits (pack of 1 vs 5/25), fire
extinguishers and fire balls (single vs set), a desk with/without pedestal.

`price unknown` rows are honest: the page had no price (brand pages, IndiaMART
enquiry pages, epson.co.in, primegodrej.com), so the price gate cannot run.

The four low-confidence rows are the closest calls: 0.50-0.70 on cards whose
title is thin or unbranded (P33, P36, P50, P53).

## Proposal (not applied - it changes behaviour)

Do not lower `MATCH_CONFIDENCE` (0.75). Two smaller options:

1. Split `possible` by reason in the UI and the eval: a `price unknown` card is
   "same product, price not shown" and can be shown as evidence; a
   `different pack` card is a genuinely different offer and should stay out of
   the price comparison.
2. Let the pooling tool (`python -m eval.adjudicate review`) decide the pack
   rows: a person says yes/no per listing, and `pool` adds the yes rows to a
   copy of the key. That keeps the strict score strict and counts the rows that
   a person agrees are the same offer.
