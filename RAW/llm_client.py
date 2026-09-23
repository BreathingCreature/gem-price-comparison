"""
Part 2 — LLM client wrapper.

Everything the pipeline needs from the NVIDIA endpoint lives here, behind
three functions: normalize_product, verify_or_extract, expand_search. All
three force JSON-only responses and go through the same retry/rate-limit
plumbing.

Ported near-verbatim from the old repo's matching/llm_attributes.py: the
_extract_json() brace-counting parser (handles markdown fences and stray
prose around the JSON) and the rate-limiter pattern (module-level last-call
timestamp, sleep to stay under the free-tier request cap). Both were
already solid — no reason to rewrite them.

What's different from the old code: no SQLite caching here (this project
is meant to be fresh-lookup, not permanently cached — see config.py's
CACHE_TTL_SECONDS, which Part 6 handles at the pipeline level instead), no
regex fallback (that's explicitly deferred — see the build spec's
"Deferred" section), and three prompts instead of one, since this pipeline
asks the LLM to do more: identity normalization, match verification with a
raw-page-reading fallback, and query expansion for the retry loop.

Reminder for anyone reading this cold: the NVIDIA endpoint is a plain text
completion API. expand_search() below is NOT the LLM browsing the internet
— it only reasons over the query/domain lists it's handed and its own
training knowledge. See the build spec doc, section 0, for why this
distinction matters.
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from config import (
    NVIDIA_BASE_URL,
    NVIDIA_MODEL,
    NVIDIA_MIN_SECONDS_BETWEEN_CALLS,
    require_nvidia_key,
    logger,
)

_last_call_ts = [0.0]
_client_singleton = [None]


# --- low-level plumbing --------------------------------------------------------

def _extract_json(raw: str) -> Optional[dict]:
    """Parse the first JSON object out of an LLM response. Handles stray
    prose before/after the object, markdown fences, and trailing text.
    Ported from the old repo's llm_attributes.py — unchanged, it was
    already correct."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

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
                    return json.loads(cleaned[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _respect_rate_limit() -> None:
    elapsed = time.time() - _last_call_ts[0]
    if elapsed < NVIDIA_MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(NVIDIA_MIN_SECONDS_BETWEEN_CALLS - elapsed)


def _get_client():
    """Lazy singleton — only requires the API key (and only imports
    `openai`) the first time an LLM call actually happens, not at module
    import time. Tests inject their own fake client instead of calling
    this at all."""
    if _client_singleton[0] is None:
        api_key = require_nvidia_key()
        from openai import OpenAI

        _client_singleton[0] = OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key)
    return _client_singleton[0]


def _complete(client, messages: list[dict], max_tokens: int) -> str:
    resp = client.chat.completions.create(
        model=NVIDIA_MODEL,
        messages=messages,
        temperature=0.0,
        max_tokens=max_tokens,
    )
    raw = resp.choices[0].message.content
    return (raw or "").strip()


def _call_llm(system_prompt: str, user_content: str, *, max_tokens: int = 700, client=None) -> dict:
    """Forces JSON-only output. Retries once with a correction message on
    unparseable output, then raises — no silent fallback, that's
    deliberately deferred scope (see module docstring)."""
    active_client = client or _get_client()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    _respect_rate_limit()
    raw = _complete(active_client, messages, max_tokens)
    _last_call_ts[0] = time.time()
    data = _extract_json(raw)
    if data is not None:
        return data

    logger.warning("LLM returned non-JSON on first try (%r) — retrying once with a correction nudge.", raw[:120])
    messages.append({"role": "assistant", "content": raw})
    messages.append(
        {
            "role": "user",
            "content": "That wasn't valid JSON. Respond with ONLY a valid JSON object, no markdown fences, no commentary.",
        }
    )
    _respect_rate_limit()
    raw2 = _complete(active_client, messages, max_tokens)
    _last_call_ts[0] = time.time()
    data2 = _extract_json(raw2)
    if data2 is not None:
        return data2

    raise RuntimeError(f"LLM did not return valid JSON after one retry. Last response: {raw2[:200]!r}")


# --- 1. normalize_product ----------------------------------------------------

_NORMALIZE_SYSTEM = """You are extracting structured product identification data from an Indian government e-marketplace (GeM) product listing, so the SAME physical product can be searched for and matched against retail marketplaces like Amazon and Flipkart.

Given the product's title, its URL category/product slug words, and whatever spec fields were found on the page, extract:
- "brand": manufacturer/brand name, proper capitalization. Null if not confidently identifiable — do not guess.
- "model": specific model name/number. Null if not identifiable.
- "key_identifiers": 2-5 short strings that MUST match for two listings to be considered the same product (brand, model, and only the specs that actually distinguish this product from a similar one — not generic descriptors like "durable" or "high quality").
- "canonical_name": a clean "Brand Model — short descriptor" string.
- "search_queries": 1-3 short, natural search strings (5-10 words each) a person would type into Amazon or Flipkart's search box to find this exact product. No GeM jargon, no tender/procurement codes, no warranty terms.

Respond with ONLY a single JSON object with exactly these five keys: brand, model, key_identifiers, canonical_name, search_queries. No markdown fences, no explanation, no other text."""


def normalize_product(gem_product: dict, *, client=None) -> dict:
    """gem_product: a GemProduct.to_dict() (see gem_extractor.py).
    Returns a NormalizedProduct dict."""
    slug_words = f"{gem_product.get('category_slug', '')} {gem_product.get('product_slug', '')}".replace("-", " ").strip()
    specs_lines = "\n".join(f"- {k}: {v}" for k, v in (gem_product.get("specs") or {}).items())

    user_content = (
        f"title: \"{gem_product.get('title', '')}\"\n"
        f"slug_words: \"{slug_words}\"\n"
        f"specs:\n{specs_lines or '(none found)'}"
    )

    data = _call_llm(_NORMALIZE_SYSTEM, user_content, client=client)

    return {
        "canonical_name": data.get("canonical_name") or gem_product.get("title", ""),
        "brand": data.get("brand") or gem_product.get("brand"),
        "model": data.get("model") or gem_product.get("model"),
        "key_identifiers": data.get("key_identifiers") or [],
        "search_queries": data.get("search_queries") or ([gem_product.get("title")] if gem_product.get("title") else []),
    }


# --- 2. verify_or_extract -----------------------------------------------------

_VERIFY_SYSTEM = """You are verifying whether a product listing found on a retail marketplace is the SAME physical product as a reference product from a government e-marketplace (GeM) listing. Minor variant differences (color, bundle packaging that doesn't change the core item) are acceptable — a genuinely different model, capacity, or spec is not a match.

You will be given the reference product's identity, and a candidate listing that may include BOTH scraper-parsed fields (title/price, which may be missing or wrong if the scraper failed) AND the raw visible text of the candidate's page. If the parsed fields look complete and trustworthy, use them. If they look missing, incomplete, or wrong, read the raw page text yourself and extract the real title and price from it instead.

Respond with ONLY a JSON object with exactly these keys:
- "is_match": true or false
- "confidence": a number from 0 to 1
- "reason": one short sentence
- "extracted_title": the title you actually used
- "extracted_price": the numeric price you actually used, or null
- "extraction_source": "scraper" if you trusted the parsed fields, "llm_raw_read" if you had to read raw_page_text yourself

No markdown fences, no other text."""


def verify_or_extract(normalized: dict, candidate: dict, *, client=None) -> dict:
    """normalized: a NormalizedProduct dict (from normalize_product).
    candidate: a CandidateResult.to_dict() (see scrapers/base.py).
    Returns a MatchDecision dict."""
    payload = {
        "reference_product": normalized,
        "candidate_structured_fields": candidate.get("structured_fields"),
        "candidate_raw_page_text": (candidate.get("raw_page_text") or "")[:4000],
    }
    user_content = json.dumps(payload, ensure_ascii=False)

    data = _call_llm(_VERIFY_SYSTEM, user_content, client=client)

    is_match = bool(data.get("is_match"))
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    price_used = None
    if is_match:
        try:
            price_used = float(data["extracted_price"]) if data.get("extracted_price") is not None else None
        except (TypeError, ValueError):
            price_used = None

    return {
        "candidate_url": candidate.get("url"),
        "source_domain": candidate.get("source_domain"),
        "is_match": is_match,
        "confidence": confidence,
        "reason": data.get("reason", ""),
        "price_used": price_used,
        "extraction_source": data.get("extraction_source", "scraper"),
    }


# --- 3. expand_search ----------------------------------------------------------

_EXPAND_SYSTEM = """A search for this product across Indian e-commerce marketplaces returned no confirmed matches using the queries and domains listed below. Suggest what to try next, based on general knowledge of Indian retail — which sites commonly stock this brand/category, and alternate ways to phrase the search.

You do NOT have live internet access. Base this only on general knowledge, not real-time information — do not claim to have checked anything.

Respond with ONLY a JSON object with exactly these keys:
- "new_queries": 1-3 alternate search strings, meaningfully different from the ones already tried (different phrasing, alternate model naming, broader or narrower terms)
- "suggested_domains": 0-3 additional Indian retail domains (e.g. "reliancedigital.in") that plausibly stock this kind of product — empty list if you don't have a genuine reason to suggest any
- "notes": one short sentence explaining your reasoning

No markdown fences, no other text."""


def expand_search(normalized: dict, tried_queries: list[str], tried_domains: list[str], *, client=None) -> dict:
    user_content = json.dumps(
        {
            "reference_product": normalized,
            "queries_already_tried": tried_queries,
            "domains_already_tried": tried_domains,
        },
        ensure_ascii=False,
    )

    data = _call_llm(_EXPAND_SYSTEM, user_content, max_tokens=400, client=client)

    return {
        "new_queries": data.get("new_queries") or [],
        "suggested_domains": data.get("suggested_domains") or [],
        "notes": data.get("notes", ""),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python llm_client.py <gem_product_url>  (runs extract_gem_product -> normalize_product)")
        sys.exit(1)

    from gem_extractor import extract_gem_product

    product = extract_gem_product(sys.argv[1])
    normalized = normalize_product(product.to_dict())
    print(json.dumps(normalized, indent=2, ensure_ascii=False))
