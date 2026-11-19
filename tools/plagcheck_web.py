"""Probe exact phrases against OpenAlex full text and arXiv metadata.

Both are free, key-less APIs; neither is a plagiarism database, they only tell
us whether a phrase already exists in the indexed scholarly literature.

    python tools/plagcheck_web.py cache/plagcheck/queries.json \
        -o cache/plagcheck/web.json [--limit 40]

Each row of the input JSON needs "id", "sentence" and "query" (see
tools/plagcheck_queries.py).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "plagcheck/1.0 (student research; contact: local)"
OPENALEX = "https://api.openalex.org/works"
ARXIV = "http://export.arxiv.org/api/query"


def get_json(url: str, timeout: float = 25.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def get_text(url: str, timeout: float = 25.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def openalex_phrase(phrase: str) -> dict:
    q = urllib.parse.quote('"%s"' % phrase)
    url = (f"{OPENALEX}?filter=fulltext.search:{q}"
           f"&per-page=3&select=id,title,doi,publication_year")
    try:
        data = get_json(url)
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - tool, keep going
        return {"error": str(exc)[:120]}
    works = data.get("results") or []
    return {
        "count": (data.get("meta") or {}).get("count", 0),
        "top": [{"title": w.get("title"), "doi": w.get("doi"),
                 "year": w.get("publication_year")} for w in works],
    }


def arxiv_phrase(phrase: str) -> dict:
    words = phrase.split()
    # arXiv wants an AND of quoted terms; keep it short
    expr = " AND ".join(f'all:"{w}"' for w in words[:6])
    url = f"{ARXIV}?search_query={urllib.parse.quote(expr)}&max_results=3"
    try:
        text = get_text(url)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120]}
    total = 0
    import re
    m = re.search(r"<opensearch:totalResults[^>]*>(\d+)<", text)
    if m:
        total = int(m.group(1))
    return {"count": total}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("queries")
    ap.add_argument("-o", "--out", default="")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--skip-arxiv", action="store_true")
    args = ap.parse_args(argv)

    with open(args.queries, encoding="utf-8") as fh:
        rows = json.load(fh)[:args.limit]
    results = []
    for i, row in enumerate(rows, 1):
        phrase = " ".join(row["query"].split()[:8])
        oa = openalex_phrase(phrase)
        ax = {"count": 0} if args.skip_arxiv else arxiv_phrase(phrase)
        results.append({"id": row["id"], "sentence": row["sentence"],
                        "phrase": phrase, "openalex": oa, "arxiv": ax})
        hit = oa.get("count", 0) or ax.get("count", 0)
        print(f"{i:>3}/{len(rows)} {'HIT ' if hit else '    '}"
              f"oa={oa.get('count', oa.get('error', '?'))} "
              f"arx={ax.get('count', ax.get('error', '?'))} :: {phrase[:70]}")
        time.sleep(0.4)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
