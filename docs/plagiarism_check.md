# Plagiarism check - research paper draft

Target: `Research_Paper_Draft.docx` (IEEE conference draft, 2,509 words,
80 paragraphs, downloaded 23 Oct 2026).
Checked: 23 Oct 2026. Tools: `tools/plagcheck_*.py`.

## 1. Result

| Measure | Original | After fixes |
|---|---|---|
| Body text (bibliography excluded), sentence level | **3.63 %** | **0.00 %** |
| Whole document incl. references | 6.9 % | 3.61 % |
| Matched against the 3 published papers on the same topic | 0.00 % | 0.00 % |
| Repeated sentences inside the paper | 0 | 0 |
| Verbatim hits from 15 exact-phrase web probes | 0 | - |

**Verdict: not plagiarised.** The only overlap found anywhere was with the
authors' own earlier project report and earlier draft of this same paper
(4 body sentences, ~82 words). Those sentences were rewritten, so the body
now matches nothing in the corpus. The remaining 3.61 % of the whole document
is the reference list, which Turnitin/iThenticate-type tools exclude.

That is well under the usual 15 % (some journals 10 %) acceptance bar.

## 2. What was compared against

22 documents, ~120 k words:

- the two papers supplied in `Related Docs/Research Papers/`
  (`66.pdf` = Vayadande et al., *An Automated Approach to Finding the Best
  Prices on GeM and Private E-Commerce Sites*, 2024; `JETIR2406357.pdf`);
- a third paper on the same problem found during the check:
  Mishra et al., *Price Comparision of Gem Products with Other
  E-Marketplaces*, IJRPR 6(5), 2025 (same university, likely supervisor's
  other group) - downloaded and added to the corpus;
- the authors' earlier draft (`Related Docs/Review 2/Paper Draft.docx`,
  `Paper Draft - Copy.docx`);
- `Project_Report_GeM_Price_Comparison.docx`;
- every `docs/*.md` and `README.md` in this repository.

## 3. Method (and its limits)

1. **Extraction** - `word/document.xml` → plain text
   (`tools/plagcheck_extract.py`); PDFs via pypdf (`tools/plagcheck_pdf.py`).
2. **N-gram matching** - the text is cut into contiguous 6-word windows. A
   sentence is *flagged* when at least half of its windows already exist
   verbatim in one source document. 6 words is slightly stricter than the
   ~8-word run that plagiarism services normally count, so this errs toward
   over-reporting (`tools/plagcheck_local.py`).
3. **Web probes** - 15 of the most distinctive 8-12 word fragments were
   searched as quoted phrases. A phrase that exists online returns the source
   page; "No search results found" means the exact wording does not exist in
   the index (`tools/plagcheck_queries.py` builds the probes).
   Probed fragments cover every section: definition of GeM, the entity
   matching definition, Magellan, prompt injection, the price gate, the
   pre-filter, the metrics, the hand check, robots.txt and the conclusion.
4. **Internal duplication** - repeated 8-word openings and repeated 8-grams
   inside the paper itself.

Limits, stated plainly:

- This is **not** Turnitin. It cannot see closed databases (Elsevier,
  Springer, IEEE Xplore full text) and it cannot detect **paraphrase**
  plagiarism - text that keeps the ideas and sentence order of a source but
  changes the words. Only a paid service with a licensed corpus can do that.
- The web probes depend on the public search index; a paper that is not
  indexed (most conference PDFs behind paywalls) cannot be found this way.
- A low score here means "no copy-paste found", not "no similarity exists".

## 4. What was changed

Only the sentences that matched the authors' own earlier report/draft
(`tools/plagcheck_apply_fixes.py`):

| Where | Before | After |
|---|---|---|
| Abstract | Simple code checks on currency, price range and confidence guard every answer. | Deterministic gates on currency, price range and confidence reject any answer that fails them. |
| §I | Checking this by hand means finding the same product on several shops. | Doing this by hand would mean locating the same item across several shops. |
| §I | This paper describes a system that takes one GeM product link and returns the matching listings on other shops, with prices and the difference to the GeM price. | The system described here accepts a single GeM product link and returns the equivalent listings found on other shops, together with their prices and the gap to the GeM price. |
| §II (Table I lead-in) | Table I compares the approaches by the properties that matter here. The cells describe each approach in general, not every single paper. | Table I sets out how these approaches differ on the properties that matter for this task. Each cell characterises an approach in general rather than one particular paper. |
| §IV-A | The key was reviewed on 22 October 2026. | The key was finalised on 22 October 2026. |

The rewritten file is `Research_Paper_Draft_plagfixed.docx` (original left
untouched). Only those 5 paragraphs differ; styles, runs, tables and figures
are unchanged.

## 5. Separate issue: AI assistance

The Acknowledgment already says parts of the software and text were produced
with an AI assistant and that the answer key was built by an AI agent. That
is *not* plagiarism, but most conferences require exactly that declaration -
keep it, and check the specific AI-use policy of the target conference
(some require a separate form).

## 6. Reproduce

```bash
python tools/plagcheck_extract.py paper.docx -o cache/plagcheck/paper.txt
python tools/plagcheck_local.py cache/plagcheck/paper.txt \
    cache/plagcheck/corpus/*.txt --json cache/plagcheck/local.json
python tools/plagcheck_queries.py cache/plagcheck/paper.txt \
    -o cache/plagcheck/queries.json
```

Raw artifacts: `cache/plagcheck/local.json`, `cache/plagcheck/local.md`,
`cache/plagcheck/queries.json`.
