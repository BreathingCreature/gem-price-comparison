"""
Part 5 — matcher.

Intentionally thin, per the build spec: a loop over candidates plus a
filter. All the real judgment lives in Part 2's verify_or_extract prompt.

One addition beyond the original sketch: returns BOTH the full set of
decisions (matched and rejected) and the confirmed subset, not just the
confirmed list. Part 8's trace viewer wants to show what happened with
every candidate, not just the ones that survived — rejecting something is
as much a "decision made" as confirming it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from config import MATCH_CONFIDENCE_THRESHOLD, PRICE_SANITY_HIGH_RATIO, PRICE_SANITY_LOW_RATIO, logger
from llm_client import verify_or_extract

# Currencies that count as "same money as GeM". Empty/missing = unknown =
# allowed through (Amazon/Flipkart always tag INR; generic scrapes the page's
# own og:price:currency). Explicit non-INR is a hard reject: a USD price that
# happens to fall inside the numeric sanity band would otherwise pass —
# $300 vs GeM ₹10,000 sits inside [1,500, 50,000].
_INR_CURRENCIES = {"", "INR", "RS", "₹", "RUPEES", "INR RUPEE", "INR RUPEES"}


@dataclass
class MatchBatchResult:
    all_decisions: list = field(default_factory=list)  # list[dict] — every MatchDecision, matched or not
    confirmed_matches: list = field(default_factory=list)  # list[dict] — is_match + above threshold, sorted by price
    skipped: list = field(default_factory=list)  # candidate urls where verify_or_extract itself failed

    def to_dict(self) -> dict:
        return {
            "all_decisions": self.all_decisions,
            "confirmed_matches": self.confirmed_matches,
            "skipped": self.skipped,
        }


def match_candidates(normalized: dict, candidates: list, *, client=None) -> MatchBatchResult:
    """normalized: a NormalizedProduct dict (from llm_client.normalize_product).
    candidates: a list of CandidateResult objects or dicts (from any of the
    Part 4 scrapers — flipkart.search()/amazon.search()'s .candidates, or
    generic.scrape()'s single result wrapped in a list)."""
    all_decisions: list[dict] = []
    skipped: list[str] = []

    for candidate in candidates:
        candidate_dict = candidate.to_dict() if hasattr(candidate, "to_dict") else candidate
        url = candidate_dict.get("url", "<unknown url>")
        # A fetch that failed has no evidence to judge — don't burn an LLM
        # call letting the model guess from the reference alone (it will).
        if candidate_dict.get("fetch_error") and not candidate_dict.get("raw_page_text") and not candidate_dict.get(
            "structured_fields"
        ):
            logger.info("skipping %s — fetch failed (%s), no evidence to verify against.", url, candidate_dict.get("fetch_error"))
            skipped.append(url)
            continue
        # Currency hard-gate BEFORE burning an LLM call: explicit non-INR
        # (e.g. generic scrape of a .com page tagged USD) can never confirm,
        # even if its numeric price falls inside the sanity band below.
        sf = candidate_dict.get("structured_fields") or {}
        currency = str(sf.get("currency") or "").strip().upper() if hasattr(sf, "get") else ""
        if currency not in _INR_CURRENCIES:
            logger.warning("currency gate rejected %s: %s is not INR — not sending to LLM.", url, currency)
            all_decisions.append(
                {
                    "candidate_url": url,
                    "source_domain": candidate_dict.get("source_domain"),
                    "is_match": False,
                    "confidence": 0.0,
                    "reason": f"[hard-rejected: currency {currency} is not INR]",
                    "price_used": None,
                    "extraction_source": "scraper",
                }
            )
            continue
        try:
            decision = verify_or_extract(normalized, candidate_dict, client=client)
        except Exception as e:
            logger.warning("verify_or_extract failed for %s: %s: %s — skipping this candidate.", url, type(e).__name__, e)
            skipped.append(url)
            continue
        all_decisions.append(decision)

    # Hard price-sanity gate. The verify prompt asks the LLM to distrust
    # wild price gaps, but it confirmed a $39.99-parsed-as-₹39.99 match at
    # 95% confidence live — so enforce the band mechanically here. A
    # decision already marked is_match=False is left untouched.
    gem_price = normalized.get("gem_price") if isinstance(normalized, dict) else None
    if gem_price is not None:
        for d in all_decisions:
            if not d.get("is_match"):
                continue
            price = d.get("price_used")
            if price is None:
                continue
            if price < gem_price * PRICE_SANITY_LOW_RATIO or price > gem_price * PRICE_SANITY_HIGH_RATIO:
                d["is_match"] = False
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

    # A match with no price is NOT confirmable — it can't enter cheapest /
    # savings arithmetic, and silently listing it made "matches" disagree
    # with "comparison". Keep the decision in all_decisions for the trace,
    # demote the URL to skipped so it reads as incomplete, not as found.
    confirmed = []
    for d in all_decisions:
        if not (d["is_match"] and d["confidence"] >= MATCH_CONFIDENCE_THRESHOLD):
            continue
        if d.get("price_used") is None:
            reason = d.get("reason") or ""
            d["reason"] = f"{reason} [demoted: match confirmed but no price extracted]".strip()
            skipped.append(d.get("candidate_url", "<unknown url>"))
            logger.warning("demoting match at %s — confirmed at %.0f%% but price_used is null.", d.get("candidate_url", "?"), d["confidence"] * 100)
            continue
        confirmed.append(d)
    confirmed.sort(key=lambda d: d["price_used"])

    return MatchBatchResult(all_decisions=all_decisions, confirmed_matches=confirmed, skipped=skipped)


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
