"""LLM-based dynamic product attribute extraction (replaces hardcoded brand
lists in query_builder.py / match_engine.py).

Why this exists: the original design used a fixed ~25-brand hardcoded list
to detect a product's brand for query-building and hard-filter matching.
That only works for brands on the list — it does not generalize to "any
product GeM might have," which is the actual project requirement. This
module replaces that fixed list with an LLM call (NVIDIA NIM, free tier)
that extracts brand/model/specs for ANY product, with the existing
regex-based logic kept as an automatic fallback if the LLM call fails or
no API key is configured — so the pipeline still works with zero external
dependency, just less precisely.

Caching: results are cached in the product_attributes table, keyed by the
GeM product's LINK (not raw_products.id, which renumbers on DB rebuild —
same reasoning as per_product_lookup.py's searched_for_gem_link design).
Each unique GeM product is sent to the LLM at most ONCE, ever.

Setup:
    pip install openai
    Get a free key at https://build.nvidia.com (no credit card) and set:
        NVIDIA_API_KEY=nvapi-...

If NVIDIA_API_KEY is not set, or the call fails for any reason, this
module transparently falls back to the existing regex-based extraction —
the pipeline keeps working either way.
"""
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

FALLBACK_SOURCE = "fallback_regex"
LLM_SOURCE = "llm"

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Any current instruct model from https://build.nvidia.com/models works —
# this task is simple extraction, not complex reasoning, so a small/fast
# model is fine. VERIFY this model id is still listed in your NVIDIA
# catalog before relying on it; swap the string below if it's gone.
NVIDIA_MODEL = "openai/gpt-oss-20b"

MIN_SECONDS_BETWEEN_CALLS = 1.6  # stays under the ~40 req/min free-tier cap
_last_call_ts = [0.0]

PROMPT_TEMPLATE = """You are extracting structured product identification data from an Indian government e-marketplace (GeM) product listing, so the SAME physical product can be searched for on a retail site like Flipkart.

Given the product's display name and its raw URL-slug words, extract:
- "brand": the manufacturer/brand name (e.g. "HP", "Nilkamal", "Godrej"). Proper capitalization. If you cannot confidently identify a real brand, return null — do not guess.
- "model": the specific model name/number (e.g. "245 G9", "Latitude 5420"). Return null if not identifiable.
- "key_specs": a list of up to 4 short strings for specs that would matter to a shopper comparing this exact product (e.g. ["16GB RAM", "512GB SSD", "Intel i5"]). Omit anything you're not confident about. Do NOT include colors, warranty info, or GeM administrative/procurement codes.
- "search_query": a short, natural search string (5-10 words) a person would actually type into Flipkart's search box to find this exact product — brand + model + the 1-2 most important specs. No GeM jargon, no tender/warranty terms, no long strings of internal codes.

Respond with ONLY a single JSON object with exactly these four keys. No markdown fences, no explanation, no other text.

Example:
name: "HP Wired Optical Mouse"
slug_words: "hp 115 wired optical mouse black"
output: {{"brand": "HP", "model": "115", "key_specs": ["Wired", "Optical"], "search_query": "HP 115 wired optical mouse"}}

Now extract for:
name: "{name}"
slug_words: "{slug_words}"
output:"""


def _slug_words(link: str) -> str:
    m = re.search(r"/([^/]+)/p-[\d-]+-cat\.html", link or "")
    return m.group(1).replace("-", " ") if m else ""


def _ensure_cache_table(con: sqlite3.Connection):
    con.execute("""
        CREATE TABLE IF NOT EXISTS product_attributes (
            gem_link TEXT PRIMARY KEY,
            brand TEXT,
            model TEXT,
            key_specs TEXT,      -- JSON-encoded list
            search_query TEXT,
            source TEXT,         -- 'llm' or 'fallback_regex'
            created_at TEXT
        )
    """)
    con.commit()


def _get_cached(con: sqlite3.Connection, link: str):
    row = con.execute(
        "SELECT brand, model, key_specs, search_query, source "
        "FROM product_attributes WHERE gem_link=?", (link,)
    ).fetchone()
    if not row:
        return None
    brand, model, key_specs_json, search_query, source = row
    return {
        "brand": brand,
        "model": model,
        "key_specs": json.loads(key_specs_json) if key_specs_json else [],
        "search_query": search_query,
        "source": source,
    }


def _save_cache(con: sqlite3.Connection, link: str, attrs: dict):
    con.execute(
        """INSERT OR REPLACE INTO product_attributes
           (gem_link, brand, model, key_specs, search_query, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (link, attrs.get("brand"), attrs.get("model"),
         json.dumps(attrs.get("key_specs") or []), attrs.get("search_query"),
         attrs.get("source"), datetime.now(timezone.utc).isoformat()),
    )
    con.commit()


def _regex_fallback(name: str, link: str) -> dict:
    """Reuses the existing, already-tested regex logic as the safety net."""
    from scrapers.query_builder import build_queries, _brand_from, raw_slug
    slug = raw_slug(link or "")
    segs = (slug or "").replace("-", " ").split()
    brand = _brand_from(segs, name) or None
    queries = build_queries(name, link)
    return {
        "brand": brand.title() if brand else None,
        "model": None,
        "key_specs": [],
        "search_query": queries[0] if queries else (name or ""),
        "source": FALLBACK_SOURCE,
    }


def _extract_json(raw: str) -> dict | None:
    """Parse the first JSON object out of an LLM response. Handles stray
    prose before/after the object, markdown fences, and trailing text."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Walk to the first '{'; try to close from there.
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _call_llm(name: str, link: str) -> dict | None:
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        print("openai package not installed (`pip install openai`) — using regex fallback.")
        return None

    elapsed = time.time() - _last_call_ts[0]
    if elapsed < MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(MIN_SECONDS_BETWEEN_CALLS - elapsed)

    client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key)
    prompt = PROMPT_TEMPLATE.format(name=name or "", slug_words=_slug_words(link))
    try:
        resp = client.chat.completions.create(
            model=NVIDIA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=700,  # needs headroom for reasoning tokens + the JSON
        )
        _last_call_ts[0] = time.time()
        raw = resp.choices[0].message.content
        if not raw or not raw.strip():
            print("NVIDIA NIM returned empty content — using regex fallback for this product.")
            return None
        raw = raw.strip()
    except Exception as e:
        print(f"NVIDIA NIM call failed ({e}) — using regex fallback for this product.")
        return None

    data = _extract_json(raw)
    if data is None:
        print(f"NVIDIA NIM returned non-JSON ({raw[:120]!r}) — using regex fallback for this product.")
        return None

    return {
        "brand": data.get("brand"),
        "model": data.get("model"),
        "key_specs": data.get("key_specs") or [],
        "search_query": data.get("search_query") or (name or ""),
        "source": LLM_SOURCE,
    }


def get_product_attributes(name: str, link: str, con: sqlite3.Connection) -> dict:
    """Main entry point. Cache-first, LLM second, regex fallback last."""
    _ensure_cache_table(con)
    cached = _get_cached(con, link)
    if cached:
        return cached
    attrs = _call_llm(name, link) or _regex_fallback(name, link)
    _save_cache(con, link, attrs)
    return attrs