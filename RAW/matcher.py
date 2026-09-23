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

from config import MATCH_CONFIDENCE_THRESHOLD, logger
from llm_client import verify_or_extract


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
        try:
            decision = verify_or_extract(normalized, candidate_dict, client=client)
        except Exception as e:
            logger.warning("verify_or_extract failed for %s: %s: %s — skipping this candidate.", url, type(e).__name__, e)
            skipped.append(url)
            continue
        all_decisions.append(decision)

    confirmed = [d for d in all_decisions if d["is_match"] and d["confidence"] >= MATCH_CONFIDENCE_THRESHOLD]
    confirmed.sort(key=lambda d: d["price_used"] if d["price_used"] is not None else float("inf"))

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
