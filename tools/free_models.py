"""Find free models that work right now, and keep the best ones.

    python tools/free_models.py                          # report only (both providers)
    python tools/free_models.py --provider openrouter --write
    python tools/free_models.py --provider nvidia --limit 12 --write
    python tools/free_models.py --provider openrouter --models a,b --json

OpenRouter's free models come from its public model list (price 0 in and 0 out,
text in and text out). NVIDIA's come from its `/models` list, filtered to chat
families because the list also carries embeddings, rerankers, vision-only and
guardrail models that cannot answer our prompts.

Each candidate gets one tiny JSON question: the "EMI card" price pick. A model
that answers it right is fast enough and obedient enough for the matcher; the
price check is also the cheapest single probe. `--write` puts the fastest
correct models (up to `--keep`, default 5) into that provider's `models` list in
`llm_providers.json` and forgets their old 404 notes.

The probe spends real quota: OpenRouter's free tier allows about 20 requests a
minute and 50 a day, so the default is to probe only 10 models there. `_Pacer`
spaces call starts per provider; keep `--workers` at 2 unless you raised the
limit.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import config  # noqa: E402
from llm import client as llm_client  # noqa: E402
from tools import check_models  # noqa: E402

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_AUTO_MODEL = "openrouter/free"
PROBE_MAX_TOKENS = config.NVIDIA_MAX_TOKENS  # same budget the matcher gives a model

PROVIDERS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "style": "plain",
        "timeout_seconds": 60,
        "probe_limit": 10,
        "keep": 5,
        "min_gap": 3.2,
        "workers": 2,
    },
    "nvidia": {
        "base_url": config.NVIDIA_BASE_URL,
        "api_key_env": "NVIDIA_API_KEY",
        "style": "nvidia",
        "timeout_seconds": config.NVIDIA_TIMEOUT_SECONDS,
        "probe_limit": 25,
        "keep": 5,
        "min_gap": 1.6,
        "workers": 3,
    },
}

# Never worth a probe: guardrail, retrieval and media-only models.
_NOT_CHAT = (
    "embed", "rerank", "retriev", "guard", "safety", "moderat", "parse",
    "vision", "vila", "fuyu", "deplot", "kosmos", "neva", "clip", "tts",
    "asr", "whisper", "ocr", "codec", "audio", "video", "riva", "nemoasr",
    "diffusion", "sd-", "flux", "lyria",
)

# Chat families NVIDIA hosts. Anything else in /models is usually a tool model.
_NVIDIA_CHAT_TOKENS = (
    "nemotron", "llama", "deepseek", "qwen", "mistral", "mixtral", "gpt-oss",
    "granite", "jamba", "dbrx", "yi-", "sea-lion", "starcoder", "gemma",
    "phi-", "magistral", "codestral", "kimi", "glm", "minimax", "baichuan",
    "internlm", "zamba", "sarvam",
)

# Best-known families first, so `--limit` keeps the ones worth having.
_NVIDIA_PREFERENCE = (
    "nemotron-3-ultra", "nemotron-3.5", "nemotron-3", "nemotron",
    "llama-3.3", "llama-3.1", "llama", "deepseek-v4", "deepseek",
    "qwen", "mistral", "mixtral", "gpt-oss", "granite", "gemma", "phi-",
)


class _Pacer:
    """Spaces call starts so a provider's per-minute limit is not tripped."""

    def __init__(self, gap: float):
        self.gap = max(0.0, float(gap))
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self.gap:
            return
        with self._lock:
            start = max(time.monotonic(), self._next)
            self._next = start + self.gap
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def _get_json(url: str, session=None, headers: dict | None = None) -> dict:
    if session is None:
        import requests

        session = requests
    resp = session.get(url, headers=headers or {}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _is_zero(value) -> bool:
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def _ascii(text: str) -> str:
    """Console-safe text: cp1252 terminals choke on the rupee sign and friends."""
    return str(text).encode("ascii", "replace").decode("ascii")


def _is_transient(exc: Exception) -> bool:
    status = str(getattr(exc, "status_code", "") or "")
    if status in ("429", "503"):
        return True
    text = str(exc)
    return "429" in text or "503" in text


def _is_text_out(entry: dict) -> bool:
    arch = entry.get("architecture") or {}
    outputs = arch.get("output_modalities") or ["text"]
    inputs = arch.get("input_modalities") or ["text"]
    return "text" in outputs and "text" in inputs


def _looks_like_chat(model_id: str, name: str = "") -> bool:
    text = f"{model_id} {name}".lower()
    return not any(token in text for token in _NOT_CHAT)


def discover_openrouter(session=None) -> list[dict]:
    """Free (0 in, 0 out), text-in/text-out models on OpenRouter."""
    data = _get_json(OPENROUTER_MODELS_URL, session)
    rows = []
    for entry in data.get("data") or []:
        model_id = str(entry.get("id") or "").strip()
        name = str(entry.get("name") or model_id)
        if not model_id or model_id == OPENROUTER_AUTO_MODEL:
            continue
        pricing = entry.get("pricing") or {}
        if not _is_zero(pricing.get("prompt")) or not _is_zero(pricing.get("completion")):
            continue
        if not _is_text_out(entry) or not _looks_like_chat(model_id, name):
            continue
        rows.append({
            "id": model_id,
            "name": name,
            "context": entry.get("context_length"),
        })
    return rows


def _nvidia_rank(model_id: str) -> tuple[int, str]:
    low = model_id.lower()
    for index, token in enumerate(_NVIDIA_PREFERENCE):
        if token in low:
            return index, model_id
    return len(_NVIDIA_PREFERENCE), model_id


def discover_nvidia(session=None, api_key: str | None = None) -> list[dict]:
    """Chat-family models from NVIDIA's /models list, best families first."""
    if api_key is None and session is None:
        api_key = config.require_nvidia_key()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = config.NVIDIA_BASE_URL.rstrip("/") + "/models"
    data = _get_json(url, session, headers)
    rows = []
    for entry in data.get("data") or []:
        model_id = str(entry.get("id") or "").strip()
        if not model_id:
            continue
        low = model_id.lower()
        if not any(token in low for token in _NVIDIA_CHAT_TOKENS):
            continue
        if not _looks_like_chat(model_id):
            continue
        rows.append({"id": model_id, "name": model_id, "context": None})
    rows.sort(key=lambda row: _nvidia_rank(row["id"]))
    return rows


def _ask_for(provider: dict, timeout: float):
    """A chat caller for one provider, reading its key from the environment."""
    from openai import OpenAI

    key_env = provider["api_key_env"]
    api_key = os.getenv(key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"{key_env} is not set")
    client = OpenAI(
        base_url=provider["base_url"],
        api_key=api_key,
        timeout=timeout,
        max_retries=0,
    )

    def ask(model: str, messages: list[dict], max_tokens: int) -> str:
        for attempt in (1, 2):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=max_tokens,
                    **llm_client._model_kwargs(model, provider.get("style") or "plain", None),
                )
                return (resp.choices[0].message.content or "").strip()
            except Exception as exc:
                if attempt == 1 and _is_transient(exc):
                    time.sleep(5)
                    continue
                raise
        return ""

    return ask


def probe(model: str, ask) -> dict:
    """One real question. Correct JSON price = usable model."""
    row = {"model": model, "ok": False, "seconds": None, "status": "ok", "error": ""}
    started = time.time()
    try:
        raw = ask(model, check_models._PRICE, PROBE_MAX_TOKENS)
    except Exception as exc:  # one dead model must not stop the report
        row["status"] = str(getattr(exc, "status_code", "") or type(exc).__name__)
        row["error"] = _ascii(str(exc))[:200]
        row["seconds"] = round(time.time() - started, 1)
        return row
    row["seconds"] = round(time.time() - started, 1)
    answer = llm_client._extract_json(raw)
    value = check_models._price_value(answer)
    row["ok"] = (
        isinstance(answer, dict)
        and value is not None
        and abs(value - check_models.EXPECTED_PRICE) < 0.5
    )
    if not row["ok"]:
        row["error"] = _ascii(raw or "(empty answer)")[:120].replace("\n", " ")
    return row


def _load_providers(path: Path) -> dict:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {"providers": data}
    return {"strategy": "failover", "providers": []}


def write_models(provider_name: str, models: list[str], path: Path | None = None) -> Path:
    """Replace one provider's `models` list, leaving the rest of the file alone."""
    if provider_name not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider_name}")
    path = Path(path or config.LLM_PROVIDERS_FILE)
    data = _load_providers(path)
    entries = data.setdefault("providers", [])
    target = next((e for e in entries if e.get("name") == provider_name), None)
    if target is None:
        defaults = PROVIDERS[provider_name]
        target = {
            "name": provider_name,
            "base_url": defaults["base_url"],
            "api_key_env": defaults["api_key_env"],
            "models": [],
            "min_seconds_between_calls": defaults["min_gap"],
            "timeout_seconds": defaults["timeout_seconds"],
            "style": defaults["style"],
        }
        entries.append(target)
    target["models"] = list(models)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def run_provider(name: str, *, models=None, limit=None, timeout=None,
                 ask=None, session=None, workers=None) -> tuple[list[dict], int]:
    """Discover (or take) candidate models, probe them, return rows + skipped."""
    settings = PROVIDERS[name]
    if models:
        candidates = [{"id": model, "name": model} for model in models]
    elif name == "openrouter":
        candidates = discover_openrouter(session)
    else:
        candidates = discover_nvidia(session)
    skipped = 0
    if not models:
        limit = int(limit or settings["probe_limit"])
        skipped = max(0, len(candidates) - limit)
        candidates = candidates[:limit]
    ids = [row["id"] for row in candidates]
    if not ids:
        return [], skipped

    timeout = float(timeout or settings["timeout_seconds"])
    ask = ask or _ask_for(settings, timeout)
    pacer = _Pacer(settings["min_gap"])

    def task(model):
        pacer.wait()
        return probe(model, ask)

    with ThreadPoolExecutor(max_workers=int(workers or settings["workers"])) as pool:
        rows = list(pool.map(task, ids))
    return rows, skipped


def _print_table(name: str, rows: list[dict], skipped: int) -> None:
    print(f"[{name}]")
    print(f"  {'model':58s} {'status':10s} {'secs':>6s}  ok")
    for row in rows:
        seconds = "-" if row["seconds"] is None else f"{row['seconds']:.1f}"
        print(f"  {row['model']:58s} {str(row['status']):10s} {seconds:>6s}  {check_models._yes(row['ok'])}")
        if row["error"]:
            print(f"      {row['error']}")
    if skipped:
        print(f"  ({skipped} more candidate(s) not probed; raise --limit to include them)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Probe free models and keep the fastest correct ones.")
    parser.add_argument("--provider", choices=("openrouter", "nvidia", "both"), default="both")
    parser.add_argument("--models", help="comma-separated ids to probe instead of discovering")
    parser.add_argument("--limit", type=int, help="max candidates to probe per provider")
    parser.add_argument("--keep", type=int, default=5, help="how many good models to write (default 5)")
    parser.add_argument("--timeout", type=float, help="seconds per probe call")
    parser.add_argument("--workers", type=int, help="parallel probes per provider")
    parser.add_argument("--write", action="store_true", help="put the good models into llm_providers.json")
    parser.add_argument("--json", action="store_true", help="print the rows as JSON")
    args = parser.parse_args(argv)

    names = ["openrouter", "nvidia"] if args.provider == "both" else [args.provider]
    models = [m.strip() for m in (args.models or "").split(",") if m.strip()] or None
    if models and len(names) > 1:
        parser.error("--models needs one --provider")

    report = {}
    written = []
    for name in names:
        rows, skipped = run_provider(
            name,
            models=models,
            limit=args.limit,
            timeout=args.timeout,
            workers=args.workers,
        )
        best = check_models.rank(rows)[: max(0, int(args.keep))]
        report[name] = {"models": rows, "best": best, "skipped": skipped}
        if not args.json:
            _print_table(name, rows, skipped)
            print("  best:", ", ".join(best) if best else "none (keep the current list)")
            print()
        if args.write and best:
            written.append((name, best))

    if args.json:
        print(json.dumps(report, indent=2))
    for name, best in written:
        path = write_models(name, best)
        dropped = check_models.forget_retired(best)
        print(f"wrote {len(best)} model(s) to {name} in {path}"
              + (f"; forgot {len(dropped)} old 404 note(s)" if dropped else ""))
    return 0 if any(part["best"] for part in report.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
