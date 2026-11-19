"""Extract plain text from .docx files without python-docx.

Usage:
    python tools/plagcheck_extract.py <file.docx> [-o out.txt]

Reads word/document.xml, keeps paragraph breaks, drops field codes and
revision marks. Stdlib only so it runs on this phone.
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _walk(el, out: list[str]) -> None:
    tag = el.tag
    if tag == W + "p":
        out.append("\n")
    elif tag == W + "t":
        out.append(el.text or "")
    elif tag == W + "tab":
        out.append("\t")
    elif tag in (W + "br", W + "cr"):
        out.append("\n")
    elif tag == W + "delText":
        return
    for child in el:
        _walk(child, out)


def docx_text(path: str | Path) -> str:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)
    chunks: list[str] = []
    _walk(root, chunks)
    text = "".join(chunks)
    # paragraphs were marked with an empty string before their runs
    lines = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def paragraphs(path: str | Path) -> list[str]:
    """One entry per non-empty Word paragraph."""
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)
    paras = []
    for p in root.iter(W + "p"):
        chunks: list[str] = []
        for t in p.iter(W + "t"):
            chunks.append(t.text or "")
        line = re.sub(r"[ \t]+", " ", "".join(chunks)).strip()
        if line:
            paras.append(line)
    return paras


def _utf8_stdout() -> None:
    """Document text is UTF-8; a Windows cp1252 console must not kill the run."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("docx")
    ap.add_argument("-o", "--out", default="")
    ap.add_argument("--paragraphs", action="store_true",
                    help="print one paragraph per line, numbered, to stdout")
    args = ap.parse_args(argv)

    if args.paragraphs:
        for i, p in enumerate(paragraphs(args.docx), 1):
            print(f"{i}\t{p}")
        return 0

    text = docx_text(args.docx)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(text)} chars)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
