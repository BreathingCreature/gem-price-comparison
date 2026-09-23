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
import random
import re
import time
from typing import Optional

from config import (
    NVIDIA_BASE_URL,
    NVIDIA_MODELS,
    NVIDIA_MIN_SECONDS_BETWEEN_CALLS,
    NVIDIA_TIMEOUT_SECONDS,
    NVIDIA_MAX_TOKENS,
    require_nvidia_key,
    logger,
)

_last_call_ts = [0.0]
_client_singleton = [None]
_working_model = [None]  # first model that actually answers, reused afterwards

# NVIDIA's free tier answers 503 "Service temporarily overloaded" in bursts.
# With the model list pinned to a single model, "fall through to next model"
# meant zero resilience — one blip killed the whole pipeline. Retry the SAME
# model a few times with growing backoff before giving up on it.
ENDPOINT_RETRY_ATTEMPTS = 3
ENDPOINT_RETRY_BACKOFF = (2.0, 5.0)  # seconds, multiplied by attempt number


# --- low-level plumbing --------------------------------------------------------

def _as_str_list(value) -> list[str]:
    """Coerce an LLM field that should be a list of strings. Models sometimes
    return a bare string ("Logitech M650 mouse") instead of ["Logitech M650
    mouse"] — downstream, queries[0] on a string would silently become the
    first CHARACTER and discovery would iterate per character. Never trust
    the model's types, only its content."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


def _as_bool(value) -> bool:
    """Strict-ish bool coercion. bool("false") is True in Python — a quoted
    "false" from the model must NOT confirm a match."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


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

        _client_singleton[0] = OpenAI(
            base_url=NVIDIA_BASE_URL,
            api_key=api_key,
            timeout=NVIDIA_TIMEOUT_SECONDS,
            max_retries=0,
        )
    return _client_singleton[0]


def _model_kwargs(model: str) -> dict:
    """Per-model request params required by the NVIDIA NIM API (see
    docs.api.nvidia.com/nim/reference/*-infer):
    - Nemotron models default stream=true (SSE) and thinking=ON — thinking
      leaks a reasoning trace into content and blows past our token budget.
      The official off-switch is chat_template_kwargs.enable_thinking=false.
    - gpt-oss supports reasoning_effort; 'low' keeps it fast — its reasoning
      goes to a separate reasoning_content field, so content stays clean.
    """
    kwargs: dict = {"stream": False}
    if model.startswith("nvidia/"):
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    elif model.startswith("openai/gpt-oss"):
        kwargs["reasoning_effort"] = "low"
    return kwargs


def _endpoint_errors() -> tuple:
    """Exception types meaning 'this model/endpoint is unavailable right now'
    — safe to fall back to the next model. Everything else (application
    errors, fakes raising RuntimeError in tests, bugs in our own code) must
    propagate immediately instead of being retried against every model."""
    from openai import APIError

    errors: tuple = (APIError, OSError)  # OSError covers Connection/TimeoutError
    try:  # this openai version may not use httpx; don't explode if absent
        import httpx

        errors += (httpx.HTTPError,)
    except ImportError:
        pass
    return errors


def _complete(client, messages: list[dict], max_tokens: int) -> str:
    """Call the LLM, falling back through the model preference list.
    The first model that answers becomes the remembered working model, so
    later calls in the same run skip cold/broken endpoints."""
    models = list(NVIDIA_MODELS)
    if _working_model[0] in models:
        models.remove(_working_model[0])
        models.insert(0, _working_model[0])

    errors: list[str] = []
    for model in models:
        for attempt in range(1, ENDPOINT_RETRY_ATTEMPTS + 1):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.1,  # >0: nemotron's schema has exclusiveMinimum 0
                    max_tokens=max_tokens,
                    **_model_kwargs(model),
                )
                raw = (resp.choices[0].message.content or "").strip()
            except _endpoint_errors() as e:
                errors.append(f"{model}: {type(e).__name__}: {e}")
                if attempt < ENDPOINT_RETRY_ATTEMPTS:
                    delay = random.uniform(*ENDPOINT_RETRY_BACKOFF) * attempt
                    logger.warning(
                        "LLM call failed on %s (%s: %s) — attempt %d/%d, retrying same model in %.1fs.",
                        model,
                        type(e).__name__,
                        str(e)[:200],
                        attempt,
                        ENDPOINT_RETRY_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                logger.warning(
                    "LLM call failed on %s after %d attempts (%s: %s) — trying next model.",
                    model,
                    ENDPOINT_RETRY_ATTEMPTS,
                    type(e).__name__,
                    e,
                )
                break  # this model is done; move to the next one
            if not raw:
                errors.append(f"{model}: empty content")
                break
            if _working_model[0] != model:
                logger.info("Using LLM model: %s", model)
                _working_model[0] = model
            return raw

    raise RuntimeError("All LLM models failed: " + " | ".join(errors))


def _call_llm(system_prompt: str, user_content: str, *, max_tokens: int = NVIDIA_MAX_TOKENS, client=None) -> dict:
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
        "key_identifiers": _as_str_list(data.get("key_identifiers")),
        "search_queries": _as_str_list(data.get("search_queries"))
        or ([gem_product.get("title")] if gem_product.get("title") else []),
        # Category/specs/price kept on the normalized identity so verify can
        # price-sanity-gate and expand knows the category — identity-only was
        # letting a ₹15k listing "match" a ₹2.7k reference at high confidence.
        "category_slug": gem_product.get("category_slug", ""),
        "specs": gem_product.get("specs") or {},
        "gem_price": gem_product.get("gem_price"),
        "gem_price_type": gem_product.get("gem_price_type", "unknown"),
    }


# --- 2. verify_or_extract -----------------------------------------------------

_VERIFY_SYSTEM = """You are verifying whether a product listing found on a retail marketplace is the SAME physical product as a reference product from a government e-marketplace (GeM) listing. Minor variant differences (color, bundle packaging that doesn't change the core item) are acceptable — a genuinely different model, capacity, or spec is not a match.

You will be given the reference product's identity (which may include its GeM price and category), a candidate listing URL/domain, and the candidate's scraper-parsed fields (title/price, which may be missing or wrong if the scraper failed) AND the raw visible text of the candidate's page. If the parsed fields look complete and trustworthy, use them. If they look missing, incomplete, or wrong, read the raw page text yourself and extract the real title and price from it instead.

Price sanity: if the reference GeM price is known and the candidate price is wildly different (roughly more than 3x or less than 1/3 of it) with no plausible explanation (genuine premium bundle, currency/unit confusion, MRP vs selling price), do not give a high confidence — either reject or lower confidence accordingly.

Respond with ONLY a JSON object with exactly these keys:
- "is_match": true or false (a real JSON boolean, not a string)
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
        "candidate_url": candidate.get("url"),
        "candidate_domain": candidate.get("source_domain"),
        "candidate_structured_fields": candidate.get("structured_fields"),
        "candidate_raw_page_text": (candidate.get("raw_page_text") or "")[:4000],
    }
    user_content = json.dumps(payload, ensure_ascii=False)

    data = _call_llm(_VERIFY_SYSTEM, user_content, client=client)

    is_match = _as_bool(data.get("is_match", False))
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
        "new_queries": _as_str_list(data.get("new_queries")),
        "suggested_domains": _as_str_list(data.get("suggested_domains")),
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
