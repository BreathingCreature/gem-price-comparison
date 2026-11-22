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

import functools
import hashlib
import json
import re
import threading
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

from core import config
from core.config import logger
from llm.client import MAX_SPEC_KEY_CHARS, MAX_SPECS_IN_PROMPT, _call_llm

# Bumped whenever the prompt or the input shape changes; part of the cache key.
PROMPT_VERSION = "v3"
MAX_FACTS = 14
SUMMARY_FACTS = 8
MIN_FACTS = 4
MAX_SUMMARY_CHARS = 90
MAX_VALUE_CHARS = 40
MIN_FACTS = 4
FALLBACK_FACTS = 8
SPEC_VALUE_CHARS = 200
HIGHLIGHT_WAIT_SECONDS = 3.0
# The card is optional: a hung call must not hold a key or connection longer
# than the wait cap by much. This is a module constant, not a config default.
LOW_PRIORITY_TIMEOUT_SECONDS = 20.0
# A title plus at most MAX_FACTS short rows is well under this; the cap
# only stops a chatty model from eating the default token budget.
MAX_TOKENS = 1200

_HIGHLIGHTS_SYSTEM = """You write the "highlights" (key facts) list for the product card of an Indian government e-marketplace (GeM) listing, so a buyer can see at a glance what matters about this product.

You are given the listing's title, brand, model, category slug, breadcrumb and spec fields. Respond with ONLY a single JSON object of exactly this shape:
{"title": "short card heading", "summary": "one shopper sentence", "facts": [{"label": "short label", "value": "short value"}]}

Think like a shopper comparing this exact product with other products in the same category. Work from the category slug and breadcrumb to decide what matters.

Worked example, printer:
Input title "Ink tank colour printer all in one Wi-Fi A4", specs include print technology ink tank, print speed 11 ppm mono and 6 ppm colour, monthly duty cycle 3000, wireless yes, duplex yes, paper size A4, page yield 6000.
Good: "Type: Ink tank", "Function: Print, scan, copy", "Colour: Colour", "Print speed: 11 ppm mono, 6 ppm colour", "Duty cycle: 3000 pages per month", "Connectivity: Wi-Fi", "Paper size: A4", "Duplex: Yes".
Bad: "Operating system: Windows 10", "RAM size: 128 MB", "Weight: 4 kg", "Connectivity: USB Port" when Wi-Fi is also on the page.

Worked example, steel almirah:
Input title "Steel almirah 4 door with locker, 18 gauge", specs include material mild steel, gauge 18, doors 4, locker yes, finish powder coated, dimensions 900 x 450 x 1850 mm.
Good: "Material: Mild steel", "Gauge: 18", "Doors: 4", "Locker: Yes", "Finish: Powder coated".
Bad: "Country of origin: India", "Catalogue id: 12345", "Delivery: Free".

Rules:
- Return 6 to 8 facts, and at most 14 entries in any case, ordered by how much they separate this product from others in its category.
- "summary" is at most 90 characters and says what the product is in shopper words, for example "Ink tank colour printer, print only, A4". It must be grounded in the page data like the facts.
- Never include: an operating system or driver compatibility list, a certificate or code, a tender or catalogue id, country of origin, packaging, a generic "USB" or "cable included" when another connection exists, dimensions and weight unless the category is furniture, luggage, appliances or fans, or anything the title already says word for word.
- Merge related rows into one fact, for example mono and colour speed into "Print speed: 11 ppm mono, 6 ppm colour".
- Every "label" and "value" is short; every "value" is under 40 characters and copied from the given title, brand, model or specs. Never invent numbers, units or claims.
- "title" is the card heading and must contain the brand or the model from the page.
- JSON only: no markdown fences, no commentary, no other keys."""

# Spec keys that are procurement bookkeeping, never a product fact. Matched
# against the label with punctuation and spaces removed, so "GeM Product Id"
# and "gem id" both land on a key here.
_NOISE_LABELS = ("seller", "delivery", "payment", "gemid", "productid", "catalogueid", "catalogid")

# Labels the card must never show, even when a model returns them and they
# are technically grounded in the page. Matching is on a punctuation-free
# lowercase form of the label.
_BLOCKED_EXACT = {"bis", "os", "is"}
_SIZE_ALLOWED = ("furniture", "luggage", "appliance", "fan", "chair", "almirah", "refrigerator", "washingmachine")
_BLOCKED_SUBSTR = (
    "operatingsystem", "oscompatibility", "drivercompatib", "countryoforigin",
    "catalogue", "catalogid", "tender", "certificate", "biscompliance",
    "bisstandard", "packaging", "delivery", "payment", "seller",
)
_CATEGORY_PRIORITY = {
    "printer": ("printtechnology", "inktank", "cartridge", "function", "colour",
                "printspeed", "dutycycle", "yield", "connectivity", "wireless",
                "papersize", "duplex", "warranty"),
    "laptop": ("processor", "cpu", "ram", "memory", "storage", "ssd", "display",
               "screen", "battery", "graphics", "warranty"),
    "desktop": ("processor", "cpu", "ram", "memory", "storage", "ssd", "display", "warranty"),
    "monitor": ("panelsize", "displaysize", "resolution", "panel", "refresh",
                "connectivity", "hdmi", "warranty"),
    "mobile": ("storage", "ram", "camera", "battery", "screen", "display", "network", "5g"),
    "tablet": ("storage", "ram", "camera", "battery", "screen", "display", "network"),
    "chair": ("material", "seat", "backrest", "armrest", "load", "warranty", "colour"),
    "furniture": ("material", "doors", "shelf", "gauge", "locker", "finish", "warranty"),
    "stationery": ("size", "pages", "grammage", "colour", "pack", "type"),
}

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
    breadcrumb = str(gem_product.get("breadcrumb") or "")
    return (
        f'title: "{title}"\n'
        f'brand: "{brand}"\n'
        f'model: "{model}"\n'
        f'category_slug: "{category}"\n'
        f'breadcrumb: "{breadcrumb}"\n'
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
        if fact is None or _is_blocked(fact["label"]):
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


def _is_blocked(label) -> bool:
    norm = _label_norm(label)
    if norm in _BLOCKED_EXACT:
        return True
    return any(marker in norm for marker in _BLOCKED_SUBSTR)


def _size_noise(label, payload: dict) -> bool:
    """Weight and dimensions only help when size decides the purchase."""
    if _label_norm(label) not in ("weight", "dimensions", "dimension"):
        return False
    text = " ".join(str(payload.get(key) or "") for key in ("category_slug", "breadcrumb", "title")).lower()
    return not any(word in text for word in _SIZE_ALLOWED)


def _category_terms(gem_product: dict) -> tuple:
    haystack = " ".join(str(gem_product.get(key) or "") for key in ("category_slug", "breadcrumb", "title")).lower()
    best: tuple = ()
    best_at = len(haystack)
    for name, terms in _CATEGORY_PRIORITY.items():
        at = haystack.find(name)
        if at >= 0 and at < best_at:
            best, best_at = terms, at
    return best


def _fallback_summary(gem_product: dict) -> str:
    title = str(gem_product.get("title") or "").strip()
    category = str(gem_product.get("category_slug") or "").replace("-", " ")
    base = title or category or "Product"
    return base[:MAX_SUMMARY_CHARS]


def _clean_summary(value, gem_product: dict) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text or len(text) > MAX_SUMMARY_CHARS or not _grounded(text, _haystack(gem_product)):
        return ""
    return text


def _gate(data, gem_product: dict) -> dict | None:
    """The LLM's answer turned into trustworthy facts and a summary, or None
    when any part fails (bad shape, wrong heading, hallucinated or over-long
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
        if fact is None or _is_blocked(fact["label"]) or _size_noise(fact["label"], gem_product) or not _grounded(fact["value"], haystack):
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
    return {"facts": kept, "summary": _clean_summary(data.get("summary"), gem_product)}


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
        for index, (key, value) in enumerate(specs.items()):
            label = str(key).strip()
            text = "" if value is None else str(value).strip()
            if not label or not text:
                continue
            norm = _label_norm(label)
            if _is_blocked(label) or _size_noise(label, payload) or any(noise in norm for noise in _NOISE_LABELS):
                continue
            row = {"label": label.title(), "value": text[:MAX_VALUE_CHARS]}
            if any(name in norm for name in _DEFERRED_LABELS):
                deferred.append(row)
            else:
                primary.append((index, row))
        terms = _category_terms(payload)
        primary.sort(key=lambda pair: (
            next((n for n, term in enumerate(terms) if term in _label_norm(pair[1]["label"])), len(terms)),
            pair[0],
        ))
        rows.extend([row for _, row in primary[: max(0, FALLBACK_FACTS - len(rows))]])
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


def _cache_get(payload: dict) -> dict | None:
    try:
        data = json.loads(_cache_path(payload).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    facts = _coerce_facts(data.get("facts"))
    if len(facts) < MIN_FACTS:
        return None
    summary = data.get("summary")
    return {"facts": facts, "summary": summary if isinstance(summary, str) else ""}


def _cache_put(payload: dict, outcome: dict) -> None:
    try:
        path = _cache_path(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = {
            "facts": outcome["facts"],
            "summary": outcome.get("summary", ""),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # a cache write must never cost the answer
        logger.debug("Could not write the highlights cache (%s: %s)", type(exc).__name__, exc)


# --- low-priority live client ----------------------------------------------------------

def _low_priority_model() -> str:
    """The fastest configured model, chosen by name, never a new config default.

    Live runs use this client only for the card facts. It bypasses the router
    on purpose: the card is optional, so it must not wait behind the model
    pool that is checking listings, and it must not spend a provider cooldown."""
    models = list(config.NVIDIA_MODELS) or [config.NVIDIA_MODEL]
    for name in models:
        lowered = name.lower()
        if "lightning" in lowered or "nano" in lowered:
            return name
    without_slow_models = [name for name in models if "gpt-oss" not in name.lower()]
    return without_slow_models[-1] if without_slow_models else models[0]


class _PinnedCompletions:
    """Force one cheap model even though llm.client._complete loops the list."""

    def __init__(self, inner, model: str):
        self._inner = inner
        self._model = model

    def create(self, **kwargs):
        kwargs["model"] = self._model
        return self._inner.chat.completions.create(**kwargs)


def low_priority_highlights_fn():
    """compute_highlights pinned to the fast configured model, or the plain
    function when no NVIDIA key is set. Router logic and config stay untouched."""
    try:
        from openai import OpenAI

        inner = OpenAI(
            base_url=config.NVIDIA_BASE_URL,
            api_key=config.require_nvidia_key(),
            timeout=min(config.NVIDIA_TIMEOUT_SECONDS, LOW_PRIORITY_TIMEOUT_SECONDS),
            max_retries=0,
        )
    except Exception as exc:
        logger.debug("Low-priority highlights client unavailable (%s: %s)", type(exc).__name__, exc)
        return compute_highlights
    model = _low_priority_model()
    logger.debug("Low-priority highlights model: %s", model)
    pinned = SimpleNamespace(
        chat=SimpleNamespace(completions=_PinnedCompletions(inner, model))
    )
    return functools.partial(compute_highlights, client=pinned)


# --- the full path --------------------------------------------------------------------

def compute_highlights(gem_product: dict, *, client=None) -> dict:
    """Cache, LLM call, grounding gate, fallback - in that order.

    Returns {"facts": [...], "summary": "...", "source": "llm" | "fallback"}
    and never raises. client=None lets the router pick the endpoint. The
    pipeline runs this on a daemon thread that always finishes, so an answer
    that arrives after the caller stopped waiting is still cached."""
    payload = {}
    try:
        payload = _snapshot(gem_product)
        cached = _cache_get(payload)
        if cached is not None:
            # Facts from an earlier grounded answer: no LLM call needed.
            return {"facts": cached["facts"], "summary": cached["summary"], "source": "llm"}
        outcome = None
        try:
            data = _call_llm(
                _HIGHLIGHTS_SYSTEM,
                _user_content(payload),
                max_tokens=MAX_TOKENS,
                purpose="highlights",
                client=client,
            )
            outcome = _gate(data, payload)
        except Exception as exc:
            logger.debug("Highlights call failed (%s: %s) - using the fallback.", type(exc).__name__, exc)
        if outcome is None:
            return {
                "facts": fallback_highlights(payload),
                "summary": _fallback_summary(payload),
                "source": "fallback",
            }
        _cache_put(payload, outcome)
        return {"facts": outcome["facts"], "summary": outcome["summary"], "source": "llm"}
    except Exception as exc:
        logger.debug("Highlights failed (%s: %s) - using the fallback.", type(exc).__name__, exc)
        return {
            "facts": fallback_highlights(payload),
            "summary": _fallback_summary(payload),
            "source": "fallback",
        }


# --- background job ---------------------------------------------------------------------

def _outcome_from_injected(result, payload: dict) -> dict:
    """Turn whatever an injected highlights_fn returned into the outcome
    shape. A plain fact list counts as the LLM path (source "llm"); the
    {"facts", "summary", "source"} dict compute_highlights returns keeps its
    source. Anything unusable falls back."""
    if isinstance(result, dict) and isinstance(result.get("facts"), list):
        facts = _coerce_facts(result.get("facts"))
        source = "llm" if result.get("source") in (None, "llm") else "fallback"
        summary = _clean_summary(result.get("summary"), payload)
    else:
        facts = _coerce_facts(result)
        source = "llm"
        summary = ""
    if not facts:
        return {
            "facts": fallback_highlights(payload),
            "summary": _fallback_summary(payload),
            "source": "fallback",
        }
    return {"facts": facts, "summary": summary, "source": source}


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
