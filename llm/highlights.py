"""LLM-made "highlights" (key facts) for the GeM product card.

The card shows a short list of the facts a buyer cares about most. An LLM
writes the list (see _HIGHLIGHTS_SYSTEM) but the answer is never trusted
as-is: the grounding gate checks that every word and digit-run of every
value appears in the page it claims to come from, that each value is short
enough for the card, and that the heading still names the brand or model.
Anything that fails is dropped; when fewer than MIN_FACTS survive, or the
call raises, returns junk or an empty list, fallback_highlights builds the
same list deterministically from the brand, model and spec rows.

The pipeline runs all of this on one daemon thread (start_highlights)
right after the normalise step, so a slow answer cannot delay the run:
finish() waits at most HIGHLIGHT_WAIT_SECONDS and then uses whatever it
has. A late answer still lands in the cache for the next run.

Nothing in this module raises: a bad answer, a broken cache file or a
caller passing junk all end at the deterministic fallback.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from core import config
from core.config import logger
from llm.client import MAX_SPEC_KEY_CHARS, MAX_SPECS_IN_PROMPT, _call_llm

# Bumped whenever the prompt or the input shape changes; part of the cache key.
PROMPT_VERSION = "v1"
MAX_FACTS = 14
MAX_VALUE_CHARS = 40
MIN_FACTS = 4
FALLBACK_FACTS = 8
SPEC_VALUE_CHARS = 200
HIGHLIGHT_WAIT_SECONDS = 3.0
# A title plus at most MAX_FACTS short rows is well under this; the cap
# only stops a chatty model from eating the default token budget.
MAX_TOKENS = 1200

_HIGHLIGHTS_SYSTEM = """You write the "highlights" (key facts) list for the product card of an Indian government e-marketplace (GeM) listing, so a buyer can see at a glance what matters about this product.

You are given the listing's title, brand, model, category slug and spec fields. Respond with ONLY a single JSON object of exactly this shape:
{"title": "short card heading", "facts": [{"label": "short label", "value": "short value"}]}

Rules:
- "facts" holds at most 14 entries, ordered by how much a buyer of this kind of product cares about them: the specs that distinguish this product come first, never filler.
- Every "label" fits the product type and is a few words at most (for example "Capacity", "Type", "Compatible With").
- Every "value" is under 40 characters and copied from the given title, brand, model or specs: never invent numbers, units or claims.
- No seller names, no catalogue or GeM product ids, no tender or procurement boilerplate, no delivery or payment text.
- Merge duplicate or near-duplicate facts into one row.
- "title" is the card heading and must contain the brand or the model from the page.
- JSON only: no markdown fences, no commentary, no other keys."""

# Spec keys that are procurement bookkeeping, never a product fact. Matched
# against the label with punctuation and spaces removed, so "GeM Product Id"
# and "gem id" both land on a key here.
_NOISE_LABELS = ("seller", "delivery", "payment", "gemid", "productid", "catalogueid", "catalogid")

# Honest but boring: kept only when the better rows do not fill the list.
_DEFERRED_LABELS = ("countryoforigin",)

_WORD_OR_DIGIT_RE = re.compile(r"[a-z]+|[0-9]+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


# --- prompt input ------------------------------------------------------------------

def _specs_for_prompt(specs: dict) -> str:
    """Same shape as llm.client._specs_for_prompt (first rows, keys and
    values cut, one honest omitted line) with this prompt's own value cap."""
    if not specs:
        return "(none found)"
    lines = []
    for key, value in list(specs.items())[:MAX_SPECS_IN_PROMPT]:
        k = str(key)[:MAX_SPEC_KEY_CHARS]
        v = str(value)[:SPEC_VALUE_CHARS]
        if not k or not v:
            continue
        lines.append(f"- {k}: {v}")
    omitted = len(specs) - len(lines)
    if omitted > 0:
        lines.append(f"- ({omitted} more specs omitted)")
    return "\n".join(lines)


def _user_content(gem_product: dict) -> str:
    specs = gem_product.get("specs")
    specs = specs if isinstance(specs, dict) else {}
    title = str(gem_product.get("title") or "")
    brand = str(gem_product.get("brand") or "")
    model = str(gem_product.get("model") or "")
    category = str(gem_product.get("category_slug") or "")
    return (
        f'title: "{title}"\n'
        f'brand: "{brand}"\n'
        f'model: "{model}"\n'
        f'category_slug: "{category}"\n'
        f"specs:\n{_specs_for_prompt(specs)}"
    )


# --- grounding gate ----------------------------------------------------------------

def _haystack(gem_product: dict) -> str:
    """Everything an answer may quote: title, spec keys and spec values,
    lowercased with punctuation folded to spaces so "IP54." matches "IP54"."""
    specs = gem_product.get("specs")
    specs = specs if isinstance(specs, dict) else {}
    parts = [str(gem_product.get("title") or "")]
    for key, value in specs.items():
        parts.append(str(key))
        parts.append(str(value))
    return _NON_ALNUM_RE.sub(" ", " ".join(parts).lower()) + " "


def _grounded(value: str, haystack: str) -> bool:
    """True when every word and every digit-run of value appears in the
    page text (normalised the same way, so case, trailing periods and
    doubled spaces never matter)."""
    tokens = _WORD_OR_DIGIT_RE.findall(value.lower())
    if not tokens:
        return False
    return all(token in haystack for token in tokens)


def _clean_fact(row) -> dict | None:
    """One fact row as the card wants it, or None when the row is junk."""
    if not isinstance(row, dict):
        return None
    label, value = row.get("label"), row.get("value")
    if not isinstance(label, str) or not isinstance(value, str):
        return None
    label, value = label.strip(), value.strip()
    if not label or not value or len(value) >= MAX_VALUE_CHARS:
        return None
    return {"label": label, "value": value}


def _coerce_facts(rows) -> list:
    """Well-formed, de-duplicated rows in order, capped at MAX_FACTS.
    Used for cache reads and injected answers; the LLM answer goes through
    _gate, which grounds every value first."""
    if not isinstance(rows, list):
        return []
    kept = []
    seen = set()
    for row in rows:
        fact = _clean_fact(row)
        if fact is None:
            continue
        key = (fact["label"].lower(), fact["value"].lower())
        if key in seen:
            continue
        seen.add(key)
        kept.append(fact)
        if len(kept) >= MAX_FACTS:
            break
    return kept


def _title_ok(raw_title, gem_product: dict) -> bool:
    """The heading must contain the brand or the model from the page. A
    page that names neither has nothing to contain, so nothing fails."""
    brand = str(gem_product.get("brand") or "").strip()
    model = str(gem_product.get("model") or "").strip()
    anchors = [anchor.lower() for anchor in (brand, model) if anchor]
    if not anchors:
        return True
    if not isinstance(raw_title, str):
        return False
    heading = raw_title.lower()
    return any(anchor in heading for anchor in anchors)


def _gate(data, gem_product: dict) -> list | None:
    """The LLM's answer turned into trustworthy facts, or None when any
    part of it fails (bad shape, wrong heading, hallucinated or over-long
    values, fewer than MIN_FACTS left standing)."""
    if not isinstance(data, dict):
        return None
    facts = data.get("facts")
    if not isinstance(facts, list):
        return None
    if not _title_ok(data.get("title"), gem_product):
        return None
    haystack = _haystack(gem_product)
    kept = []
    seen = set()
    for row in facts:
        fact = _clean_fact(row)
        if fact is None or not _grounded(fact["value"], haystack):
            continue
        key = (fact["label"].lower(), fact["value"].lower())
        if key in seen:
            continue
        seen.add(key)
        kept.append(fact)
        if len(kept) >= MAX_FACTS:
            break
    if len(kept) < MIN_FACTS:
        return None
    return kept


# --- deterministic fallback ---------------------------------------------------------

def _label_norm(label) -> str:
    return _NON_ALNUM_RE.sub("", str(label).lower())


def fallback_highlights(gem_product: dict) -> list:
    """The list this module can always build: brand first, then model,
    then the spec rows a buyer actually reads, at most FALLBACK_FACTS.
    Procurement noise (seller, ids, delivery, payment) is dropped and
    "country of origin" is held back until the better rows fall short of
    a full list. Values stay non-empty; labels are the raw spec keys
    title-cased. Never raises."""
    try:
        payload = gem_product if isinstance(gem_product, dict) else {}
        rows = []
        brand = str(payload.get("brand") or "").strip()
        model = str(payload.get("model") or "").strip()
        if brand:
            rows.append({"label": "Brand", "value": brand[:MAX_VALUE_CHARS]})
        if model:
            rows.append({"label": "Model", "value": model[:MAX_VALUE_CHARS]})
        specs = payload.get("specs")
        specs = specs if isinstance(specs, dict) else {}
        primary = []
        deferred = []
        for key, value in specs.items():
            label = str(key).strip()
            text = "" if value is None else str(value).strip()
            if not label or not text:
                continue
            norm = _label_norm(label)
            if any(noise in norm for noise in _NOISE_LABELS):
                continue
            row = {"label": label.title(), "value": text[:MAX_VALUE_CHARS]}
            if any(name in norm for name in _DEFERRED_LABELS):
                deferred.append(row)
            else:
                primary.append(row)
        rows.extend(primary[: max(0, FALLBACK_FACTS - len(rows))])
        if len(rows) < FALLBACK_FACTS:
            rows.extend(deferred[: FALLBACK_FACTS - len(rows)])
        return rows[:FALLBACK_FACTS]
    except Exception as exc:  # never raise out of this module
        logger.debug("fallback_highlights failed (%s: %s)", type(exc).__name__, exc)
        return []


# --- cache ---------------------------------------------------------------------------

def _snapshot(gem_product) -> dict:
    """A private copy of what this module reads, so the pipeline's dict is
    never mutated from the highlights thread."""
    if not isinstance(gem_product, dict):
        return {}
    payload = dict(gem_product)
    specs = payload.get("specs")
    payload["specs"] = dict(specs) if isinstance(specs, dict) else {}
    return payload


def _cache_specs(specs: dict) -> dict:
    out = {}
    for key, value in specs.items():
        k = str(key).strip()
        v = "" if value is None else str(value).strip()
        if k and v:
            out[k] = v[:SPEC_VALUE_CHARS]
    return out


def _digest(payload: dict) -> str:
    blob = json.dumps(
        {
            "title": str(payload.get("title") or ""),
            "specs": _cache_specs(payload.get("specs") or {}),
            "version": PROMPT_VERSION,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_path(payload: dict) -> Path:
    # config.CACHE_DIR is read here, at call time: tests point it at a
    # throwaway folder after this module is imported.
    return config.CACHE_DIR / "highlights" / f"{_digest(payload)}.json"


def _cache_get(payload: dict) -> list | None:
    try:
        data = json.loads(_cache_path(payload).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    facts = _coerce_facts(data.get("facts"))
    return facts if len(facts) >= MIN_FACTS else None


def _cache_put(payload: dict, facts: list) -> None:
    try:
        path = _cache_path(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = {"facts": facts, "saved_at": datetime.now(timezone.utc).isoformat()}
        path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # a cache write must never cost the answer
        logger.debug("Could not write the highlights cache (%s: %s)", type(exc).__name__, exc)


# --- the full path --------------------------------------------------------------------

def compute_highlights(gem_product: dict, *, client=None) -> dict:
    """Cache, LLM call, grounding gate, fallback - in that order.

    Returns {"facts": [...], "source": "llm" | "fallback"} and never
    raises. client=None lets the router pick the endpoint. The pipeline
    runs this on a daemon thread that always finishes, so an answer that
    arrives after the caller stopped waiting is still cached."""
    payload = {}
    try:
        payload = _snapshot(gem_product)
        cached = _cache_get(payload)
        if cached is not None:
            # Facts from an earlier grounded answer: no LLM call needed.
            return {"facts": cached, "source": "llm"}
        facts = None
        try:
            data = _call_llm(
                _HIGHLIGHTS_SYSTEM,
                _user_content(payload),
                max_tokens=MAX_TOKENS,
                purpose="highlights",
                client=client,
            )
            facts = _gate(data, payload)
        except Exception as exc:
            logger.debug("Highlights call failed (%s: %s) - using the fallback.", type(exc).__name__, exc)
        if facts is None:
            return {"facts": fallback_highlights(payload), "source": "fallback"}
        _cache_put(payload, facts)
        return {"facts": facts, "source": "llm"}
    except Exception as exc:
        logger.debug("Highlights failed (%s: %s) - using the fallback.", type(exc).__name__, exc)
        return {"facts": fallback_highlights(payload), "source": "fallback"}


# --- background job ---------------------------------------------------------------------

def _outcome_from_injected(result, payload: dict) -> dict:
    """Turn whatever an injected highlights_fn returned into the outcome
    shape. A plain fact list counts as the LLM path (source "llm"); the
    {"facts", "source"} dict compute_highlights returns keeps its source.
    Anything unusable falls back."""
    if isinstance(result, dict) and isinstance(result.get("facts"), list):
        facts = _coerce_facts(result.get("facts"))
        source = "llm" if result.get("source") in (None, "llm") else "fallback"
    else:
        facts = _coerce_facts(result)
        source = "llm"
    if not facts:
        return {"facts": fallback_highlights(payload), "source": "fallback"}
    return {"facts": facts, "source": source}


class HighlightsJob:
    """One background highlights computation on a single daemon thread.

    .done says whether the thread finished, .result() hands back the
    {"facts", "source"} dict once it has (None before that), and
    .wait(seconds) blocks at most that long and reports whether the
    answer arrived."""

    def __init__(self, gem_product: dict, *, client=None, highlights_fn=None):
        self._gem_product = gem_product
        self._client = client
        self._highlights_fn = highlights_fn
        self._outcome: dict | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="gem-highlights", daemon=True)

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def start(self) -> None:
        self._thread.start()

    def result(self) -> dict | None:
        return self._outcome

    def wait(self, seconds: float) -> bool:
        return self._done.wait(seconds)

    def _run(self) -> None:
        # Any exception inside the thread becomes the fallback; the event
        # is set last so wait() only returns True once the outcome exists.
        try:
            if self._highlights_fn is None:
                outcome = compute_highlights(self._gem_product, client=self._client)
            else:
                outcome = _outcome_from_injected(
                    self._highlights_fn(dict(self._gem_product)),
                    self._gem_product,
                )
        except Exception as exc:
            logger.debug("Highlights job failed (%s: %s) - using the fallback.", type(exc).__name__, exc)
            outcome = {"facts": fallback_highlights(self._gem_product), "source": "fallback"}
        self._outcome = outcome
        self._done.set()


def start_highlights(gem_product: dict, *, client=None, highlights_fn=None) -> HighlightsJob:
    """Copy what this module needs out of gem_product, compute the
    highlights on one daemon thread and return the job handle.

    highlights_fn, when given, replaces this module's own path and is
    called as highlights_fn(gem_product_dict); None runs
    compute_highlights (cache, LLM, gate, fallback) with `client`
    (client=None: the router picks the endpoint)."""
    payload = _snapshot(gem_product)
    job = HighlightsJob(payload, client=client, highlights_fn=highlights_fn)
    job.start()
    return job
