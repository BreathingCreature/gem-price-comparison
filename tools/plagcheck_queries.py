"""Pick the sentences of a paper that are worth a web exact-phrase probe.

Writes JSON: [{"id", "sentence", "query"}] where query is an 8-word window
chosen to be as distinctive as possible (most non-stopwords, fewest digits).

    python tools/plagcheck_queries.py paper.txt -o cache/plagcheck/queries.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STOP = {
    "a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "nor", "but",
    "if", "then", "than", "that", "this", "these", "those", "is", "are", "was",
    "were", "be", "been", "being", "do", "does", "did", "doing", "have", "has",
    "had", "having", "it", "its", "it's", "as", "at", "by", "from", "with",
    "without", "into", "onto", "over", "under", "about", "against", "between",
    "among", "during", "before", "after", "above", "below", "up", "down", "out",
    "off", "again", "further", "once", "here", "there", "when", "where", "why",
    "how", "all", "any", "both", "each", "few", "more", "most", "other", "some",
    "such", "no", "not", "only", "own", "same", "so", "too", "very", "can",
    "will", "just", "should", "now", "i", "you", "he", "she", "they", "we",
    "me", "him", "her", "them", "my", "your", "our", "their", "what", "which",
    "who", "whom", "whose", "while", "because", "although", "though", "since",
    "until", "unless", "upon", "per", "via", "within", "across", "toward",
    "towards", "also", "may", "might", "must", "shall", "would", "could",
}


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9\-']*", text)


def sentences(text: str) -> list[str]:
    out = []
    for block in re.split(r"\n+", text):
        for raw in re.split(r"(?<=[.!?])\s+(?=[A-Z(\"'])", block.strip()):
            raw = raw.strip()
            if len(words(raw)) >= 9:
                out.append(raw)
    return out


def best_window(sent: str, size: int = 8) -> str:
    w = words(sent)
    if len(w) <= size:
        return " ".join(w)
    best, best_score = "", -1.0
    for i in range(len(w) - size + 1):
        win = w[i:i + size]
        score = 0.0
        for token in win:
            low = token.lower()
            if low in STOP:
                continue
            score += 1.0
            if any(c.isdigit() for c in token):
                score -= 0.4
            if len(token) > 7:
                score += 0.3
            if token[0].isupper():
                score += 0.1
        if score > best_score:
            best, best_score = " ".join(win), score
    return best


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target")
    ap.add_argument("-o", "--out", default="")
    ap.add_argument("--max", type=int, default=0)
    args = ap.parse_args(argv)

    text = Path(args.target).read_text(encoding="utf-8", errors="ignore")
    rows = []
    for i, sent in enumerate(sentences(text), 1):
        q = best_window(sent)
        if len(words(q)) < 6:
            continue
        rows.append({"id": i, "sentence": sent, "query": q})
    if args.max:
        rows = rows[:args.max]
    blob = json.dumps(rows, indent=2)
    if args.out:
        Path(args.out).write_text(blob, encoding="utf-8")
        print(f"wrote {args.out} ({len(rows)} queries)")
    else:
        print(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
