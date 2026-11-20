"""Local plagiarism / similarity check for a document.

Compares the target text against every corpus text given (plain .txt files)
with word n-grams (default n=6):

  * sentence level -- a sentence is "copied" when most of its n-grams already
    exist in one corpus document (ratio >= --threshold, default 0.5)
  * word level     -- share of the target's words that sit inside a matched
    n-gram with any corpus document (the number usually reported as a
    "similarity index")

Stdlib only.  Usage:

    python tools/plagcheck_local.py TARGET.txt CORPUS/*.txt \
        [--n 6] [--threshold 0.5] [--json out.json] [--md out.md]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WORD_RE = re.compile(r"[a-z0-9]+")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")

# abbreviations that must not end a sentence
ABBREV = {
    "al", "e.g", "i.e", "fig", "figs", "eq", "eqs", "no", "vol", "pp", "p",
    "vs", "etc", "resp", "approx", "dr", "prof", "mr", "ms", "mrs", "inc",
    "ltd", "co", "st", "cf", "sec", "ch", "ref", "refs", "tab", "ed", "eds",
}


def normalize(text: str) -> str:
    return " ".join(WORD_RE.findall(text.lower()))


def words(text: str) -> list[str]:
    return WORD_RE.findall(text.lower())


def ngrams(seq: list[str], n: int) -> list[tuple[str, ...]]:
    return [tuple(seq[i:i + n]) for i in range(len(seq) - n + 1)]


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for block in re.split(r"\n+", text):
        block = block.strip()
        if not block:
            continue
        for raw in SENT_SPLIT.split(block):
            raw = raw.strip()
            if not raw:
                continue
            # glue back a split after a known abbreviation
            if out:
                tail = out[-1].rstrip(".").rsplit(" ", 1)[-1].lower()
                if tail in ABBREV and not out[-1].endswith(("!", "?")):
                    out[-1] = out[-1] + " " + raw
                    continue
            out.append(raw)
    return out


def load_corpus(paths: list[Path], n: int = 6) -> dict[str, dict]:
    corpus: dict[str, dict] = {}
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            print(f"skip {p}: {exc}", file=sys.stderr)
            continue
        seq = words(text)
        if len(seq) < 20:
            continue
        corpus[str(p)] = {"words": seq, "ngrams": set(ngrams(seq, n))}
    return corpus


def check(target_text: str, corpus: dict[str, dict], n: int,
          threshold: float) -> dict:
    target_seq = words(target_text)
    target_ngrams = ngrams(target_seq, n)
    # position -> matched doc names, for word coverage
    coverage = [False] * len(target_ngrams)
    hits: list[dict] = []

    for sent in sentences(target_text):
        seq = words(sent)
        if len(seq) < 8:
            continue
        grams = ngrams(seq, n)
        if not grams:
            continue
        best_name, best_ratio, best_hit = "", 0.0, 0
        for name, doc in corpus.items():
            hit = sum(1 for g in grams if g in doc["ngrams"])
            ratio = hit / len(grams)
            if ratio > best_ratio:
                best_name, best_ratio, best_hit = name, ratio, hit
        if best_ratio >= threshold:
            hits.append({
                "sentence": sent,
                "words": len(seq),
                "source": best_name,
                "ratio": round(best_ratio, 3),
                "matched_ngrams": best_hit,
                "total_ngrams": len(grams),
            })

    for i, g in enumerate(target_ngrams):
        if any(g in doc["ngrams"] for doc in corpus.values()):
            coverage[i] = True
    covered = sum(coverage)
    word_coverage = (covered / len(target_ngrams)) if target_ngrams else 0.0

    matched_words = sum(h["words"] for h in hits)
    total_words = sum(len(words(s)) for s in sentences(target_text))
    return {
        "target_words": len(target_seq),
        "target_sentences": len(sentences(target_text)),
        "corpus_documents": len(corpus),
        "ngram": n,
        "threshold": threshold,
        "sentence_matches": hits,
        "sentences_matched": len(hits),
        "words_in_matched_sentences": matched_words,
        "word_coverage_by_ngram": round(word_coverage, 4),
        "sentence_plagiarism_percent": round(
            100.0 * matched_words / total_words, 2) if total_words else 0.0,
    }


def render_md(result: dict, target: Path) -> str:
    lines = [
        f"# Plagiarism check - {target.name}",
        "",
        f"- target words: {result['target_words']}",
        f"- target sentences (>= 8 words counted): {result['target_sentences']}",
        f"- corpus documents: {result['corpus_documents']}",
        f"- n-gram size: {result['ngram']}, match bar: {result['threshold']}",
        f"- **sentence-level similarity: "
        f"{result['sentence_plagiarism_percent']}% of words**",
        f"- word coverage by n-gram: "
        f"{result['word_coverage_by_ngram'] * 100:.2f}%",
        f"- flagged sentences: {result['sentences_matched']}",
        "",
        "| # | ratio | source | sentence |",
        "|---|---|---|---|",
    ]
    for i, h in enumerate(result["sentence_matches"], 1):
        src = Path(h["source"]).name
        sent = h["sentence"].replace("|", "\\|")
        if len(sent) > 220:
            sent = sent[:217] + "..."
        lines.append(f"| {i} | {h['ratio']} | {src} | {sent} |")
    lines.append("")
    return "\n".join(lines)


def _utf8_stdout() -> None:
    """Document text is UTF-8; a Windows cp1252 console must not kill the run."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target")
    ap.add_argument("corpus", nargs="+")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--json", default="")
    ap.add_argument("--md", default="")
    args = ap.parse_args(argv)

    target = Path(args.target)
    text = target.read_text(encoding="utf-8", errors="ignore")
    corpus = load_corpus([Path(p) for p in args.corpus], args.n)
    result = check(text, corpus, args.n, args.threshold)
    result["target"] = str(target)

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    if args.md:
        Path(args.md).write_text(render_md(result, target), encoding="utf-8")

    print(f"target: {target}  words={result['target_words']}  "
          f"corpus={result['corpus_documents']} docs")
    print(f"sentence-level similarity: "
          f"{result['sentence_plagiarism_percent']}% of words "
          f"({result['sentences_matched']} sentences flagged)")
    print(f"word coverage by {args.n}-gram: "
          f"{result['word_coverage_by_ngram'] * 100:.2f}%")
    for h in result["sentence_matches"]:
        print(f"\n  [{h['ratio']}] {Path(h['source']).name}\n    {h['sentence'][:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
