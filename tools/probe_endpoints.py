"""Check every LLM endpoint with one tiny message.

    python -m tools.probe_endpoints
    python -m tools.probe_endpoints --json

Sends "Reply with the single word: OK" to every endpoint in
llm_providers.json and prints OK with the round-trip time, or the real
error (bad key, wrong model name, rate limit, empty answer). Disabled
endpoints are listed with their reason. Nothing is changed or fixed here —
this only reports, so a broken provider is found in 30 seconds instead of
half-way through an evaluation.

Exit code: 0 when every enabled endpoint answered, 1 when any failed,
2 when llm_providers.json itself could not be read.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from llm import client as _client
from llm.router import load_endpoints

MESSAGES = [{"role": "user", "content": "Reply with the single word: OK"}]
_RATE_LIMIT_HEADER = __import__("re").compile(r"rate|retry|reset|remaining|limit", __import__("re").IGNORECASE)


def _response_headers(resp):
    headers = getattr(resp, "headers", None)
    if headers is None:
        headers = getattr(getattr(resp, "_response", None), "headers", None)
    return headers or {}


def _finish_reason(resp):
    try:
        return resp.choices[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return None


def _rate_limit_header_names(resp):
    """Names only. A header value could contain account data, so it is never read."""
    return sorted(str(name) for name in _response_headers(resp) if _RATE_LIMIT_HEADER.search(str(name)))


def _make_client(endpoint):
    from openai import OpenAI

    key = (os.getenv(endpoint.api_key_env) or "").strip()
    return OpenAI(base_url=endpoint.base_url, api_key=key, timeout=endpoint.timeout, max_retries=0)


def probe_endpoint(endpoint, *, client_factory=None) -> dict:
    """One tiny call to one endpoint. Never raises — returns a row dict."""
    if not endpoint.enabled:
        return {"endpoint": endpoint.name, "ok": False, "disabled": True, "error": endpoint.disabled_reason}
    factory = client_factory or _make_client
    started = time.perf_counter()
    try:
        client = factory(endpoint)
        resp = client.chat.completions.create(
            model=endpoint.model,
            messages=MESSAGES,
            temperature=0.1,  # >0: nemotron's schema has exclusiveMinimum 0
            max_tokens=8,
            **_client._model_kwargs(endpoint.model, endpoint.style, endpoint.request_options),
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:  # the whole point of the tool: report the real error
        return {
            "endpoint": endpoint.name,
            "ok": False,
            "seconds": round(time.perf_counter() - started, 2),
            "error": f"{type(e).__name__}: {str(e)[:160]}",
        }
    seconds = round(time.perf_counter() - started, 2)
    if not raw:
        return {"endpoint": endpoint.name, "ok": False, "seconds": seconds,
                "finish_reason": _finish_reason(resp),
                "rate_limit_headers": _rate_limit_header_names(resp),
                "error": "empty answer (a reasoning model may need more max_tokens or request_options)"}
    return {"endpoint": endpoint.name, "ok": True, "seconds": seconds, "reply": raw[:40],
            "finish_reason": _finish_reason(resp),
            "rate_limit_headers": _rate_limit_header_names(resp)}


def _line(row: dict) -> str:
    if row.get("disabled"):
        return f"  {row['endpoint']:<48} off      {row['error']}"
    if row["ok"]:
        limits = ", ".join(row.get("rate_limit_headers") or []) or "none"
        return (f"  {row['endpoint']:<48} OK       {row['seconds']:.2f}s  {row['reply']!r}"
                f"  finish={row.get('finish_reason')}  rate-limit headers: {limits}")
    return f"  {row['endpoint']:<48} FAILED   {row['error']}"


def probe_all(endpoints, *, client_factory=None, log=print) -> list[dict]:
    """Probe each endpoint in order, printing a line per endpoint as it goes."""
    rows = []
    for endpoint in endpoints:
        row = probe_endpoint(endpoint, client_factory=client_factory)
        rows.append(row)
        if log:
            log(_line(row))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="print the rows as JSON instead of lines")
    args = ap.parse_args(argv)

    try:
        endpoints, strategy = load_endpoints()
    except Exception as e:
        print(f"cannot read the provider file: {type(e).__name__}: {e}")
        return 2

    if not args.json:
        print(f"probing {len(endpoints)} endpoint(s) (strategy: {strategy})")
    rows = probe_all(endpoints, log=None if args.json else print)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))

    ok = sum(1 for r in rows if r["ok"])
    failed = sum(1 for r in rows if not r["ok"] and not r.get("disabled"))
    disabled = sum(1 for r in rows if r.get("disabled"))
    print(f"{ok} ok, {failed} failed, {disabled} disabled")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
