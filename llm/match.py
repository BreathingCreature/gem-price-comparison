"""
The matcher: verdicts in, honest matches out.

Intentionally thin, per the build spec: a loop over candidates plus a
filter. All the real judgment lives in the verify_or_extract prompt.

One addition beyond the original sketch: returns BOTH the full set of
decisions (matched and rejected) and the confirmed subset, not just the
confirmed list. The trace wants to show what happened with
every candidate, not just the ones that survived — rejecting something is
as much a "decision made" as confirming it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial

from core import events
from core.config import (
    GATES_ENABLED,
    LLM_WORKERS,
    MATCH_CONFIDENCE_THRESHOLD,
    PRICE_PREGATE,
    PRICE_SANITY_HIGH_RATIO,
    PRICE_SANITY_LOW_RATIO,
    REUSE_VERDICTS,
    STRICT_MODE,
    logger,
)
from core.parallel import run_many
from llm.client import verify_or_extract
from llm.router import GatewayError

# Currencies that count as "same money as GeM". Empty/missing = unknown =
# allowed through (Amazon/Flipkart always tag INR; generic scrapes the page's
# own og:price:currency). Explicit non-INR is a hard reject: a USD price that
# happens to fall inside the numeric sanity band would otherwise pass —
# $300 vs GeM ₹10,000 sits inside [1,500, 50,000].
_INR_CURRENCIES = {"", "INR", "RS", "₹", "RUPEES", "INR RUPEE", "INR RUPEES"}


@dataclass
class MatchBatchResult:
    all_decisions: list = field(default_factory=list)  # list[dict] — every MatchDecision, matched or not
    confirmed_matches: list = field(default_factory=list)  # list[dict] — outcome "match", sorted by price
    skipped: list = field(default_factory=list)  # candidate urls with no usable verdict (kept for old callers)
    possible_matches: list = field(default_factory=list)  # outcome "possible" — the same product may be right
    not_checked: list = field(default_factory=list)  # [{"url", "reason"}] — the LLM could not judge it

    def to_dict(self) -> dict:
        return {
            "all_decisions": self.all_decisions,
            "confirmed_matches": self.confirmed_matches,
            "skipped": self.skipped,
            "possible_matches": self.possible_matches,
            "not_checked": self.not_checked,
        }


# Stage 6.3: one word per judged listing, so the pipeline and the result can
# say "we could not check this" instead of pretending it was rejected.
OUTCOME_MATCH = "match"
OUTCOME_POSSIBLE = "possible"
OUTCOME_NO_MATCH = "no_match"

# The fields that make up one verdict. A reused verdict keeps these and takes
# the current listing's URL/domain, so a cross-shop reuse cannot show the
# wrong link.
_VERDICT_FIELDS = ("is_match", "confidence", "reason", "price_used", "extraction_source")


def _verdict_key(normalized: dict, candidate_dict: dict) -> tuple:
    """Same product + same listing title + same price = the same question.
    The LLM answer cannot depend on which shop's page it was copied from."""
    fields = candidate_dict.get("structured_fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    return (
        str(normalized.get("canonical_name") or "").strip().lower(),
        str(normalized.get("model") or "").strip().lower(),
        normalized.get("gem_price"),
        str(fields.get("title") or "").strip().lower(),
        str(fields.get("price") or "").strip().lower(),
    )


def _decision_verdict(decision: dict) -> dict:
    return {name: decision.get(name) for name in _VERDICT_FIELDS}


def _cand_dict(candidate) -> dict:
    return candidate.to_dict() if hasattr(candidate, "to_dict") else candidate


def _apply_scraper_price(decision: dict, candidate_dict: dict) -> None:
    """Stage 6.4: the card's price is the price. The LLM only reads pages,
    the scraper read the card — when they disagree the scraper wins, and the
    difference stays in the trace instead of being silently overwritten."""
    fields = candidate_dict.get("structured_fields") or {}
    scraper_price = _price_number(fields.get("price")) if isinstance(fields, dict) else None
    if scraper_price is None:
        return
    llm_price = decision.get("price_used")
    if llm_price is not None and abs(float(llm_price) - scraper_price) > 0.01:
        decision["llm_price_seen"] = llm_price
        decision["price_note"] = f"scraper price {scraper_price} kept over LLM price {llm_price}"
        logger.info(
            "price at %s: kept the scraper's %s over the LLM's %s.",
            decision.get("candidate_url", "?"),
            scraper_price,
            llm_price,
        )
    decision["price_used"] = scraper_price
    decision["price_from"] = "scraper"


def _outcome(decision: dict, threshold: float) -> str:
    """match / possible / no_match for one judged listing (Stage 6.3)."""
    reason = decision.get("reason") or ""
    if "[hard-rejected:" in reason:
        return OUTCOME_NO_MATCH
    if "different pack" in reason.lower():
        return OUTCOME_POSSIBLE
    is_match = bool(decision.get("is_match"))
    confidence = float(decision.get("confidence") or 0.0)
    price = decision.get("price_used")
    if is_match and confidence >= threshold and price is not None:
        return OUTCOME_MATCH
    if is_match and price is None:
        return OUTCOME_POSSIBLE
    if not is_match and 0.5 <= confidence < threshold:
        return OUTCOME_POSSIBLE
    return OUTCOME_NO_MATCH


def _why_possible(decision: dict, threshold: float) -> str:
    reason = (decision.get("reason") or "").strip()
    if "different pack" in reason.lower():
        return "different pack"
    if bool(decision.get("is_match")) and decision.get("price_used") is None:
        return "price unknown"
    confidence = float(decision.get("confidence") or 0.0)
    if confidence < threshold:
        return f"confidence {confidence:.2f} below {threshold:.2f}"
    return reason or "possible"


def _emit_verdict(decision: dict) -> None:
    events.emit(
        "candidate_verdict",
        url=decision.get("candidate_url"),
        domain=decision.get("source_domain"),
        is_match=bool(decision.get("is_match")),
        confidence=decision.get("confidence"),
        reason=decision.get("reason"),
        price=decision.get("price_used"),
    )


def _price_number(value) -> float | None:
    """structured_fields prices arrive as floats or as strings like
    '1,299.00' — the price pre-gate needs a number, and anything it cannot
    parse must NOT be treated as outside the band (never guess a number)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def match_candidates(normalized: dict, candidates: list, *, client=None, verdict_cache=None) -> MatchBatchResult:
    """normalized: a NormalizedProduct dict (from llm_client.normalize_product).
    candidates: a list of CandidateResult objects or dicts (from any of the
    The shop scrapers — flipkart.search()/amazon.search()'s .candidates, or
    generic.scrape()'s single result wrapped in a list).

    Listings are verified LLM_WORKERS at a time (GEM_LLM_WORKERS, default 4).
    GEM_STRICT=1 keeps the old one-at-a-time path and turns the pre-filter
    off upstream. The decisions and the events keep the candidate order
    either way.

    verdict_cache (optional dict) lets several shops share one run's verdicts:
    the same listing title + price for the same product is never sent to the
    LLM twice. Without it, one batch still dedupes against itself when
    GEM_REUSE_VERDICTS is on (the default)."""
    all_decisions: list[dict] = []
    skipped: list[str] = []
    not_checked: list[dict] = []
    outcomes: dict = {}  # position -> ("decision", dict) / ("skipped", (url, reason)) / ("not_checked", (url, reason))
    to_verify: list = []  # (position, candidate_dict, verdict key) — the ones that need the LLM
    waiting_reuse: list = []  # duplicates of a listing already queued in this batch
    reuse = bool(REUSE_VERDICTS)
    if reuse and verdict_cache is None:
        verdict_cache = {}
    gem_price = normalized.get("gem_price") if isinstance(normalized, dict) else None

    for position, candidate in enumerate(candidates, start=1):
        candidate_dict = candidate.to_dict() if hasattr(candidate, "to_dict") else candidate
        url = candidate_dict.get("url", "<unknown url>")
        shown = candidate_dict.get("structured_fields")
        shown = shown if isinstance(shown, dict) else {}
        events.emit(
            "candidate_checking",
            url=url,
            domain=candidate_dict.get("source_domain"),
            title=shown.get("title"),
            price=shown.get("price"),
            index=position,
            total=len(candidates),
        )
        # A fetch that failed has no evidence to judge — don't burn an LLM
        # call letting the model guess from the reference alone (it will).
        if candidate_dict.get("fetch_error") and not candidate_dict.get("raw_page_text") and not candidate_dict.get(
            "structured_fields"
        ):
            logger.info("skipping %s — fetch failed (%s), no evidence to verify against.", url, candidate_dict.get("fetch_error"))
            outcomes[position] = ("skipped", (url, f"fetch failed: {candidate_dict.get('fetch_error')}"))
            continue
        # Currency hard-gate BEFORE burning an LLM call: explicit non-INR
        # (e.g. generic scrape of a .com page tagged USD) can never confirm,
        # even if its numeric price falls inside the sanity band below.
        sf = candidate_dict.get("structured_fields") or {}
        currency = str(sf.get("currency") or "").strip().upper() if hasattr(sf, "get") else ""
        if GATES_ENABLED and currency not in _INR_CURRENCIES:
            logger.warning("currency gate rejected %s: %s is not INR — not sending to LLM.", url, currency)
            outcomes[position] = (
                "decision",
                {
                    "candidate_url": url,
                    "source_domain": candidate_dict.get("source_domain"),
                    "is_match": False,
                    "confidence": 0.0,
                    "reason": f"[hard-rejected: currency {currency} is not INR]",
                    "price_used": None,
                    "extraction_source": "scraper",
                },
            )
            continue
        # Price pre-gate (GEM_PRICE_PREGATE): Amazon/Flipkart cards already
        # carry the price — if it is outside the hard gate's band the LLM call
        # would be thrown away anyway. Tied to GATES_ENABLED: with the gates
        # off nothing would reject the listing, so nothing is skipped.
        if PRICE_PREGATE and GATES_ENABLED and gem_price is not None:
            price = _price_number(sf.get("price"))
            if price is not None and (price < gem_price * PRICE_SANITY_LOW_RATIO or price > gem_price * PRICE_SANITY_HIGH_RATIO):
                reason = f"[hard-rejected: price {price} outside {PRICE_SANITY_LOW_RATIO}-{PRICE_SANITY_HIGH_RATIO}x GeM {gem_price}]"
                events.emit("candidate_gate_rejected", url=url, reason=reason)
                logger.warning("price pre-gate rejected %s: %s — not sending to the LLM.", url, reason)
                outcomes[position] = (
                    "decision",
                    {
                        "candidate_url": url,
                        "source_domain": candidate_dict.get("source_domain"),
                        "is_match": False,
                        "confidence": 0.0,
                        "reason": reason,
                        "price_used": price,
                        "extraction_source": "scraper",
                    },
                )
                continue
        key = _verdict_key(normalized, candidate_dict) if reuse else None
        cached = verdict_cache.get(key) if reuse and key is not None else None
        if cached is not None:
            # Already answered in this run: same verdict, this listing's link.
            decision = {
                "candidate_url": url,
                "source_domain": candidate_dict.get("source_domain"),
                **cached,
                "reused_verdict": True,
            }
            outcomes[position] = ("decision", decision)
            events.emit("candidate_reused", url=url, domain=candidate_dict.get("source_domain"))
            continue
        if reuse and any(item[2] == key for item in to_verify):
            waiting_reuse.append((position, candidate_dict, key))
            continue
        to_verify.append((position, candidate_dict, key))

    def _verify(item):
        position, candidate_dict, key = item
        url = candidate_dict.get("url", "<unknown url>")
        try:
            decision = verify_or_extract(normalized, candidate_dict, client=client)
        except GatewayError as e:
            # The gateway itself is out of reach (busy or used up) — that is
            # "not checked", not "rejected".
            logger.warning("no LLM answer for %s: %s — not checked.", url, e)
            return position, ("not_checked", (url, str(e)))
        except Exception as e:
            logger.warning("verify_or_extract failed for %s: %s: %s — skipping this candidate.", url, type(e).__name__, e)
            return position, ("skipped", (url, f"{type(e).__name__}: {e}"))
        if reuse and key is not None:
            verdict_cache[key] = _decision_verdict(decision)
        return position, ("decision", decision)

    # Several listings at once when GEM_LLM_WORKERS > 1; run_many keeps the
    # results in candidate order, so the report below reads the same either way.
    workers = 1 if STRICT_MODE else LLM_WORKERS
    for position, outcome in run_many([partial(_verify, item) for item in to_verify], workers=workers):
        outcomes[position] = outcome

    # Duplicates that waited for the first copy: take its verdict. If the
    # first one failed there is no verdict to reuse — report it as skipped,
    # never as "not found".
    for position, candidate_dict, key in waiting_reuse:
        url = candidate_dict.get("url", "<unknown url>")
        cached = verdict_cache.get(key) if verdict_cache is not None else None
        if cached is None:
            outcomes[position] = ("skipped", (url, "duplicate listing had no verdict to reuse"))
            continue
        outcomes[position] = (
            "decision",
            {
                "candidate_url": url,
                "source_domain": candidate_dict.get("source_domain"),
                **cached,
                "reused_verdict": True,
            },
        )

    for position in sorted(outcomes):
        kind, payload = outcomes[position]
        if kind == "not_checked":
            url, reason = payload
            skipped.append(url)  # old callers treat these as "no verdict"
            not_checked.append({"url": url, "reason": reason})
            events.emit("candidate_skipped", url=url, reason=f"not checked: {reason}")
        elif kind == "skipped":
            url, reason = payload
            skipped.append(url)
            events.emit("candidate_skipped", url=url, reason=reason)
        else:
            decision = payload
            if 0 < position <= len(candidates):
                _apply_scraper_price(decision, _cand_dict(candidates[position - 1]))
            all_decisions.append(decision)
            _emit_verdict(decision)

    # Hard price-sanity gate. The verify prompt asks the LLM to distrust
    # wild price gaps, but it confirmed a $39.99-parsed-as-₹39.99 match at
    # 95% confidence live — so enforce the band mechanically here. A
    # decision already marked is_match=False is left untouched.
    if gem_price is not None and GATES_ENABLED:
        for d in all_decisions:
            if not d.get("is_match"):
                continue
            price = d.get("price_used")
            if price is None:
                continue
            if price < gem_price * PRICE_SANITY_LOW_RATIO or price > gem_price * PRICE_SANITY_HIGH_RATIO:
                d["is_match"] = False
                events.emit(
                    "candidate_gate_rejected",
                    url=d.get("candidate_url"),
                    reason=f"price {price} is outside {PRICE_SANITY_LOW_RATIO}-{PRICE_SANITY_HIGH_RATIO}x the GeM price {gem_price}",
                )
                reason = d.get("reason") or ""
                d["reason"] = (
                    f"{reason} [hard-rejected: price {price} outside "
                    f"{PRICE_SANITY_LOW_RATIO}-{PRICE_SANITY_HIGH_RATIO}x GeM {gem_price}]"
                ).strip()
                logger.warning(
                    "price-sanity gate rejected match at %s: price %s vs GeM %s (band %s-%sx).",
                    d.get("candidate_url", "?"),
                    price,
                    gem_price,
                    PRICE_SANITY_LOW_RATIO,
                    PRICE_SANITY_HIGH_RATIO,
                )

    # Stage 6.3: every judged listing gets exactly one outcome. A "match" is
    # only confirmed with a real price (it must enter the cheapest/savings
    # arithmetic); a same-product verdict with no price is `possible`, which
    # is honest and still visible, instead of the old silent demotion.
    threshold = MATCH_CONFIDENCE_THRESHOLD
    for d in all_decisions:
        d["outcome"] = _outcome(d, threshold)
        if d["outcome"] == OUTCOME_POSSIBLE:
            d["why"] = _why_possible(d, threshold)

    confirmed = [d for d in all_decisions if d["outcome"] == OUTCOME_MATCH]
    confirmed.sort(key=lambda d: d["price_used"])
    possible = [d for d in all_decisions if d["outcome"] == OUTCOME_POSSIBLE]
    possible.sort(
        key=lambda d: (
            d.get("price_used") is None,
            d.get("price_used") if d.get("price_used") is not None else 0.0,
            -float(d.get("confidence") or 0.0),
        )
    )

    return MatchBatchResult(
        all_decisions=all_decisions,
        confirmed_matches=confirmed,
        skipped=skipped,
        possible_matches=possible,
        not_checked=not_checked,
    )


if __name__ == "__main__":
    # Small smoke test with a fake client, since this needs no real network
    # or API key to demonstrate the filter/sort logic.
    class _FakeMessage:
        def __init__(self, content):
            self.content = content

    class _FakeChoice:
        def __init__(self, content):
            self.message = _FakeMessage(content)

    class _FakeResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]

    class _FakeCompletions:
        def __init__(self, responses):
            self._responses = list(responses)

        def create(self, **kwargs):
            return _FakeResponse(self._responses.pop(0))

    class _FakeChat:
        def __init__(self, responses):
            self.completions = _FakeCompletions(responses)

    class _FakeClient:
        def __init__(self, *responses):
            self.chat = _FakeChat(responses)

    import json

    fake = _FakeClient(
        '{"is_match": true, "confidence": 0.9, "reason": "match", "extracted_title": "A", "extracted_price": 1500, "extraction_source": "scraper"}',
        '{"is_match": true, "confidence": 0.95, "reason": "match", "extracted_title": "B", "extracted_price": 999, "extraction_source": "scraper"}',
        '{"is_match": false, "confidence": 0.8, "reason": "different model", "extracted_title": "C", "extracted_price": 500, "extraction_source": "scraper"}',
    )
    demo_candidates = [
        {"url": "https://x.com/1", "source_domain": "flipkart.com", "structured_fields": {"title": "A"}, "raw_page_text": ""},
        {"url": "https://x.com/2", "source_domain": "amazon.in", "structured_fields": {"title": "B"}, "raw_page_text": ""},
        {"url": "https://x.com/3", "source_domain": "amazon.in", "structured_fields": {"title": "C"}, "raw_page_text": ""},
    ]
    result = match_candidates({"canonical_name": "Demo"}, demo_candidates, client=fake)
    print(json.dumps(result.to_dict(), indent=2))
