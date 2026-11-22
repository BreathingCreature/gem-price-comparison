"""Round 7 Task 1: fallback and gate behaviour on ten real shaped fixtures."""
import json
from pathlib import Path

import pytest

from llm import highlights

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "highlights_cases.json").read_text(encoding="utf-8")
)


def _labels(rows):
    return {row["label"].lower() for row in rows}


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_fallback_uses_the_buyer_facts_and_drops_the_noise(case):
    labels = _labels(highlights.fallback_highlights(case["gem_product"]))
    for label in case["must_include"]:
        assert label.lower() in labels, (case["name"], label, labels)
    for label in case["must_not_include"]:
        assert label.lower() not in labels, (case["name"], label, labels)
    assert len(labels) <= highlights.FALLBACK_FACTS


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_recorded_model_answer_is_grounded_and_blocklisted(case):
    data = json.loads(case["answer"])
    outcome = highlights._gate(data, case["gem_product"])
    assert outcome is not None, case["name"]
    labels = _labels(outcome["facts"])
    for label in case["must_include"]:
        assert label.lower() in labels, (case["name"], label, labels)
    for label in case["must_not_include"]:
        assert label.lower() not in labels, (case["name"], label, labels)
    assert outcome["summary"]
    assert len(outcome["summary"]) <= highlights.MAX_SUMMARY_CHARS


def test_prompt_asks_for_a_summary_examples_and_no_filler():
    system = highlights._HIGHLIGHTS_SYSTEM
    assert '"summary"' in system
    assert "Worked example, printer" in system
    assert "Worked example, steel almirah" in system
    assert "operating system" in system
    assert "at most 14 entries" in system


def test_prompt_version_was_bumped():
    assert highlights.PROMPT_VERSION == "v3"
