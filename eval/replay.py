"""Record the shops once, replay the same listings many times (kit 20).

    python -m eval.replay --key eval/answer_key.csv --record baseline
    python -m eval.replay --key eval/answer_key.csv --replay baseline --label parallel-on

A --record run is a normal live run (same as eval/run_eval.py) that also
saves every shop answer — Flipkart search, Amazon search, discovery and the
generic page fetches — under eval/recordings/<name>/, one small file per
product. A --replay run never touches a shop: the same questions are answered
from the recording, so two experiments are compared on exactly the same
listings and only the setting under test differs.

The LLM is still called unless LLM_CACHE=1. Queries should be identical
between record and replay because they come out of the recorded run; if the
LLM produces a different query anyway, the replay says exactly which recorded
answer is missing instead of quietly comparing different listings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from eval import run_eval
from eval.score import load_key
from scrapers._base import CandidateResult, SearchScrapeResult

DEFAULT_RESULTS_DIR = "eval/results"
DEFAULT_RECORDINGS_DIR = "eval/recordings"


class ReplayDivergence(RuntimeError):
    """The pipeline asked the shops something the recording has no answer to."""


def make_key(kind: str, *parts) -> str:
    """One recording key. JSON so queries exactly identify their answer."""
    return json.dumps([kind, *parts], ensure_ascii=False)


def product_slug(gem_url: str) -> str:
    """Readable file name for one product's recording, stable per URL."""
    base = re.sub(r"[^a-z0-9]+", "-", (gem_url or "").lower()).strip("-")[-80:]
    digest = hashlib.sha1((gem_url or "").encode("utf-8")).hexdigest()[:10]
    return f"{base}-{digest}" if base else digest


def _as_dict(value):
    return value.to_dict() if hasattr(value, "to_dict") else value


class RecordingStore:
    """One product's recorded shop answers, kept on disk and in memory."""

    def __init__(self, rec_dir, gem_url: str):
        self.gem_url = gem_url
        self.path = Path(rec_dir) / f"{product_slug(gem_url)}.json"
        self.responses: dict = {}

    def load(self) -> dict:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.responses.update(data.get("responses") or {})
        return self.responses

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "gem_url": self.gem_url,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "responses": self.responses,
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def wrap_shops(
    store: RecordingStore,
    mode: str,
    *,
    flipkart_search_fn,
    amazon_search_fn,
    discover_fn,
    generic_scrape_fn,
) -> dict:
    """The four shop functions of core.pipeline, wrapped for recording or replay.

    Record mode answers from the real shop and saves the answer; replay mode
    answers from the saved one and never calls the shop. Search results are
    rebuilt into the real dataclasses so the pipeline cannot tell the
    difference; discovery results stay plain dicts (the pipeline accepts both).
    """
    store.load()
    rebuilt: dict = {}

    def _lookup(key: str):
        if key not in store.responses:
            raise ReplayDivergence(
                f"replay divergence: nothing was recorded for {key}. The pipeline asked for "
                "something the recording has no answer to (usually a different query produced by "
                "the LLM). Re-record with --record, or set LLM_CACHE=1 so the same queries come back."
            )
        return store.responses[key]

    def make_search(kind: str, inner):
        def call(query: str):
            key = make_key(kind, query)
            if mode == "record":
                result = inner(query)
                store.responses[key] = result.to_dict()
                return result
            data = _lookup(key)
            if key not in rebuilt:
                rebuilt[key] = SearchScrapeResult(
                    source_domain=data["source_domain"],
                    query=data["query"],
                    candidates=[CandidateResult(**c) for c in data.get("candidates") or []],
                    blocked=bool(data.get("blocked")),
                    pages_scraped=data.get("pages_scraped") or 0,
                    error=data.get("error"),
                )
            return rebuilt[key]

        return call

    def discover(queries, allowed_domains=None):
        key = make_key("discover", list(queries), list(allowed_domains) if allowed_domains is not None else None)
        if mode == "record":
            results = discover_fn(queries, allowed_domains)
            store.responses[key] = [_as_dict(d) for d in results]
            return results
        return _lookup(key)

    def scrape(url: str):
        key = make_key("scrape", url)
        if mode == "record":
            result = generic_scrape_fn(url)
            store.responses[key] = _as_dict(result)
            return result
        data = _lookup(key)
        if key not in rebuilt:
            rebuilt[key] = CandidateResult(**data)
        return rebuilt[key]

    return {
        "flipkart_search_fn": make_search("flipkart", flipkart_search_fn),
        "amazon_search_fn": make_search("amazon", amazon_search_fn),
        "discover_fn": discover,
        "generic_scrape_fn": scrape,
    }


def _default_pipeline(gem_url, **kwargs):
    from core.pipeline import run_pipeline

    return run_pipeline(gem_url, **kwargs)


def _default_shops() -> dict:
    from discovery.discover import discover_for_queries
    from scrapers.amazon import search as amazon_search
    from scrapers.flipkart import search as flipkart_search
    from scrapers.generic import scrape as generic_scrape

    return {
        "flipkart_search_fn": flipkart_search,
        "amazon_search_fn": amazon_search,
        "discover_fn": discover_for_queries,
        "generic_scrape_fn": generic_scrape,
    }


def make_run_fn(rec_dir, mode: str, *, pipeline_fn=None, shops=None):
    """A run_fn for eval.run_eval.run_all that records or replays the shops."""
    pipeline = pipeline_fn or _default_pipeline
    base_shops = shops or _default_shops()

    def run_fn(gem_url, refresh=False, on_event=None):
        store = RecordingStore(rec_dir, gem_url)
        wrapped = wrap_shops(store, mode, **base_shops)
        try:
            return pipeline(gem_url, refresh=refresh, on_event=on_event, **wrapped)
        finally:
            if mode == "record":
                store.save()

    return run_fn


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True)
    mode_group = ap.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--record", metavar="NAME", help="run live and save every shop answer under this name")
    mode_group.add_argument("--replay", metavar="NAME", help="run without touching the shops, answering from this recording")
    ap.add_argument("--label", help="name of this run's results file (default: NAME for --record, NAME-replay for --replay)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="comma-separated product ids")
    ap.add_argument("--dir", default=DEFAULT_RESULTS_DIR)
    ap.add_argument("--rec-dir", default=DEFAULT_RECORDINGS_DIR)
    args = ap.parse_args(argv)

    name = args.record or args.replay
    mode = "record" if args.record else "replay"
    label = args.label or (name if mode == "record" else f"{name}-replay")
    out = Path(args.dir) / f"{label}.jsonl"
    meta = Path(args.dir) / f"{label}.meta.json"
    rows = load_key(args.key)
    run_eval.write_meta(meta, label, extra={"replay": {"mode": mode, "recording": name}})
    only = set(args.only.split(",")) if args.only else None
    run_eval.run_all(rows, out, run_fn=make_run_fn(Path(args.rec_dir) / name, mode), limit=args.limit, only=only)
    print(f"results: {out}\nscore them with: python -m eval.score --key {args.key} --results {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
