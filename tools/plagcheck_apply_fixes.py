"""Apply the plagiarism-review sentence rewrites to a .docx copy.

Only the sentences flagged by tools/plagcheck_local.py are touched; every
other paragraph, run and table cell is left byte-identical.  The original
file is never modified.

Needs python-docx (installed outside this repo, e.g.
PYTHONPATH=/data/data/com.foxdebug.acode/cache/opencode/pylibs):

    PYTHONPATH=... python tools/plagcheck_apply_fixes.py SRC.docx DST.docx
"""
from __future__ import annotations

import argparse
import sys

from docx import Document

# (old fragment, new fragment) -- old must appear exactly once in the document
FIXES: list[tuple[str, str]] = [
    (
        "Simple code checks on currency, price range and confidence guard every answer.",
        "Deterministic gates on currency, price range and confidence reject any "
        "answer that fails them.",
    ),
    (
        "Checking this by hand means finding the same product on several shops.",
        "Doing this by hand would mean locating the same item across several shops.",
    ),
    (
        "This paper describes a system that takes one GeM product link and returns "
        "the matching listings on other shops, with prices and the difference to "
        "the GeM price.",
        "The system described here accepts a single GeM product link and returns "
        "the equivalent listings found on other shops, together with their prices "
        "and the gap to the GeM price.",
    ),
    (
        "Table I compares the approaches by the properties that matter here. "
        "The cells describe each approach in general, not every single paper.",
        "Table I sets out how these approaches differ on the properties that "
        "matter for this task. Each cell characterises an approach in general "
        "rather than one particular paper.",
    ),
    (
        "The key was reviewed on 22 October 2026.",
        "The key was finalised on 22 October 2026.",
    ),
]


def replace_in_paragraph(paragraph, old: str, new: str) -> bool:
    """Replace `old` inside one run (keeps that run's formatting)."""
    for run in paragraph.runs:
        if old in run.text:
            run.text = run.text.replace(old, new)
            return True
    return False


def replace_in_tables(document, old: str, new: str) -> bool:
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    if replace_in_paragraph(p, old, new):
                        return True
    return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src")
    ap.add_argument("dst")
    args = ap.parse_args(argv)

    doc = Document(args.src)
    missing: list[str] = []
    for old, new in FIXES:
        done = False
        for p in doc.paragraphs:
            if replace_in_paragraph(p, old, new):
                done = True
                break
        if not done:
            done = replace_in_tables(doc, old, new)
        if not done:
            missing.append(old)

    if missing:
        print("NOT FOUND (document unchanged):")
        for m in missing:
            print("  -", m[:90])
        return 1

    doc.save(args.dst)
    print(f"wrote {args.dst} ({len(FIXES)} sentences rewritten)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
