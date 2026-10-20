"""Check which NVIDIA models answer right now, and keep the good ones.

    python tools/check_models.py
    python tools/check_models.py --models a,b --timeout 60
    python tools/check_models.py --write

Each model gets two tiny JSON questions: a product match and an "EMI card"
price pick. The table shows who answered, how fast, and whether the answer
was right. `--write` copies the good models (fastest first) into the NVIDIA
entry of `llm_providers.json` and forgets their old 404 notes.

NVIDIA adds and removes free models often. A 404 already retires that model
on disk by itself, so the next run simply uses the next model in the list;
this tool is for picking the list again after a bigger change.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import config  # noqa: E402
from llm import client as llm_client  # noqa: E402

# Models worth trying, best guess first. Dead names are kept on purpose: they
# show up as 404 in the report and cost one fast call.
CANDIDATES = (
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "openai/gpt-oss-20b",
    "deepseek-ai/deepseek-v4.1-flash",
)

_MATCH = [
    {"role": "system", "content": "You reply with JSON only, no prose."},
    {"role": "user", "content": (
        'Reference product: "Logitech M650 Wireless Mouse, Graphite, Silent Clicks"\n'
        'Listing title: "Logitech M650 Signature Wireless Mouse - Graphite, Silent Clicks, Bluetooth/Logi Bolt"\n'
        'Listing price: 1799\n'
        'Is this the same product? Reply exactly: {"same_product": true or false, '
        '"confidence": 0.0 to 1.0, "reason": "at most 8 words"}'
    )},
]
_PRICE = [
    {"role": "system", "content": "You reply with JSON only, no prose."},
    {"role": "user", "content": (
        'A shop card says: "No Cost EMI from \u20b999/month  \u20b91,299  \u20b92,499".\n'
        'What is the actual selling price in rupees? Reply exactly: {"price": <number>}'
    )},
]
EXPECTED_PRICE = 1299.0


def _ask(model: str, messages: list[dict], max_tokens: int, timeout: float) -> str:
    """One real call to the NVIDIA endpoint."""
    from openai import OpenAI

    client = OpenAI(
        base_url=config.NVIDIA_BASE_URL,
        api_key=config.require_nvidia_key(),
        timeout=timeout,
        max_retries=0,
    )
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.1,  # >0: nemotron's schema has exclusiveMinimum 0
        max_tokens=max_tokens,
        **llm_client._model_kwargs(model, "nvidia", None),
    )
    return (resp.choices[0].message.content or "").strip()


def _price_value(answer) -> float | None:
    if not isinstance(answer, dict):
        return None
    value = answer.get("price")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").replace(",", "").replace("\u20b9", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def check_model(model: str, *, ask=None, timeout: float = 45.0) -> dict:
    """Two questions, one row. `ask` is injectable so tests stay offline."""
    ask = ask or (lambda m, messages, max_tokens: _ask(m, messages, max_tokens, timeout))
    row = {
        "model": model,
        "ok": False,
        "json_ok": False,
        "match_ok": False,
        "price_ok": False,
        "seconds": None,
        "status": "ok",
        "error": "",
    }
    started = time.time()
    try:
        raw_match = ask(model, _MATCH, 256)
        raw_price = ask(model, _PRICE, 128)
    except Exception as exc:  # a dead or retired model must not stop the report
        row["status"] = str(getattr(exc, "status_code", "") or type(exc).__name__)
        row["error"] = str(exc)[:200]
        row["seconds"] = round(time.time() - started, 1)
        return row
    row["seconds"] = round(time.time() - started, 1)
    match = llm_client._extract_json(raw_match)
    price = llm_client._extract_json(raw_price)
    row["json_ok"] = isinstance(match, dict) and isinstance(price, dict)
    row["match_ok"] = bool(match) and match.get("same_product") is True
    price_value = _price_value(price)
    row["price_ok"] = price_value is not None and abs(price_value - EXPECTED_PRICE) < 0.5
    row["ok"] = row["json_ok"] and row["match_ok"] and row["price_ok"]
    return row


def rank(rows: list[dict]) -> list[str]:
    """The models that answered both questions right, fastest first."""
    good = [row for row in rows if row.get("ok")]
    good.sort(key=lambda row: row.get("seconds") or 1e9)
    return [row["model"] for row in good]


def write_config(models: list[str], path: Path | None = None) -> Path:
    """Put `models` into the NVIDIA entry of the provider file."""
    path = Path(path or config.LLM_PROVIDERS_FILE)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {"providers": data}
    else:
        data = {"strategy": "failover", "providers": []}
    providers = data.setdefault("providers", [])
    target = None
    for entry in providers:
        if entry.get("base_url", "").rstrip("/") == config.NVIDIA_BASE_URL.rstrip("/") or entry.get("name") == "nvidia":
            target = entry
            break
    if target is None:
        target = {
            "name": "nvidia",
            "base_url": config.NVIDIA_BASE_URL,
            "api_key_env": "NVIDIA_API_KEY",
            "style": "nvidia",
            "min_seconds_between_calls": config.NVIDIA_MIN_SECONDS_BETWEEN_CALLS,
            "timeout_seconds": config.NVIDIA_TIMEOUT_SECONDS,
        }
        providers.insert(0, target)
    target["models"] = list(models)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def forget_retired(models: list[str]) -> list[str]:
    """Drop the 404 notes of these models so they are tried again."""
    path = Path(config.CACHE_DIR) / "llm_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    retired = state.get("retired") if isinstance(state, dict) else None
    if not isinstance(retired, dict):
        return []
    dropped = [name for name in retired if any(name.endswith("/" + model) for model in models)]
    for name in dropped:
        retired.pop(name, None)
    if dropped:
        path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    return dropped


def _print_table(rows: list[dict]) -> None:
    print(f"{'model':52s} {'status':10s} {'secs':>6s}  json match price")
    for row in rows:
        seconds = "-" if row["seconds"] is None else f"{row['seconds']:.1f}"
        print(
            f"{row['model']:52s} {str(row['status']):10s} {seconds:>6s}  "
            f"{_yes(row['json_ok']):4s} {_yes(row['match_ok']):5s} {_yes(row['price_ok'])}"
        )
        if row["error"]:
            print(f"    {row['error']}")


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check which NVIDIA models answer right now.")
    parser.add_argument("--models", help="comma-separated model names (default: the built-in list)")
    parser.add_argument("--timeout", type=float, default=45.0, help="seconds per call (default 45)")
    parser.add_argument("--write", action="store_true", help="put the good models into llm_providers.json")
    parser.add_argument("--json", action="store_true", help="print the rows as JSON")
    args = parser.parse_args(argv)

    models = [m.strip() for m in (args.models or "").split(",") if m.strip()] or list(CANDIDATES)
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(lambda model: check_model(model, timeout=args.timeout), models))
    best = rank(rows)

    if args.json:
        print(json.dumps({"models": rows, "best": best}, indent=2))
    else:
        _print_table(rows)
        print()
        if best:
            print("good models, fastest first:", ", ".join(best))
        else:
            print("no model answered both questions right; keep the current list")
    if args.write and best:
        path = write_config(best)
        dropped = forget_retired(best)
        print(f"wrote {len(best)} model(s) to {path}" + (f"; forgot {len(dropped)} old 404 note(s)" if dropped else ""))
    return 0 if best else 1


if __name__ == "__main__":
    sys.exit(main())
