"""PDF text extraction (pypdf from cache/plagcheck/pylibs)."""
from __future__ import annotations

import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "cache" / "plagcheck" / "pylibs"
sys.path.insert(0, str(LIB))

from pypdf import PdfReader  # noqa: E402


def main(argv: list[str]) -> int:
    for src in argv:
        p = Path(src)
        out = p.with_suffix(".txt")
        reader = PdfReader(str(p))
        pages = []
        for i, page in enumerate(reader.pages, 1):
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # pragma: no cover
                pages.append("")
                print(f"{p.name} page {i}: {exc}", file=sys.stderr)
        text = "\n".join(pages)
        Path(out).write_text(text, encoding="utf-8")
        print(f"{out} ({len(text)} chars, {len(reader.pages)} pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
