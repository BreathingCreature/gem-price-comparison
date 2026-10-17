"""A small on-disk memory of LLM answers.

When the same question (same messages, same token limit, same purpose) is
asked again, the saved answer is returned and no LLM call is made. This
matters for the evaluation: a test of one setting against another repeats
thousands of identical questions, and the free LLM plans only allow about a
thousand calls a day.

Off by default. Turn it on with LLM_CACHE=1 before starting the program.
Do NOT leave it on when you measure how fast the project is: a cache hit is
instant, so the timing would be fake.

Only answers that were valid JSON are saved (llm/client.py calls put()
after parsing), so a bad answer is never replayed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from core.config import LLM_CACHE_DIR as CACHE_DIR
from core.config import LLM_CACHE_ENABLED as ENABLED


def key_for(messages: list[dict], max_tokens: int, purpose: str) -> str:
    raw = json.dumps({"m": messages, "t": max_tokens, "p": purpose}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(key: str) -> Optional[dict]:
    path = Path(CACHE_DIR) / f"{key}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    answer = data.get("answer") if isinstance(data, dict) else None
    return answer if isinstance(answer, dict) else None


def put(key: str, answer: dict) -> None:
    try:
        folder = Path(CACHE_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{key}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"answer": answer}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that cannot be written is just a cache that does not help


def clear() -> int:
    """Delete every saved answer. Returns how many were deleted."""
    folder = Path(CACHE_DIR)
    n = 0
    if folder.exists():
        for p in folder.glob("*.json"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n
