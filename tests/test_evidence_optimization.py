from copy import deepcopy

import pytest

import prompts
import retrieval


def context(identifier=1, text="The fee is INR 100. Bamboo is exempt.", **overrides):
    return {"id": identifier, "document_id": "rules", "source": "rules.pdf", "section_heading": "Rule 1",
            "chunk_index": identifier, "text": text, "score": .9, "base_score": .9, "metadata": {}, **overrides}


def test_duplicates_are_not_reintroduced_to_fill_limit():
    first = context()
    duplicate = context(2)
    exception = context(3, "Provided that approval is required for bamboo.")
    assert retrieval.diversify_contexts([first, duplicate, exception], 3) == [first, exception]


@pytest.mark.parametrize("overrides", [
    {"text": "The fee is INR 1000. Bamboo is exempt."},
    {"text": "The fee is INR 100. Bamboo is not exempt."},
    {"document_id": "other"}, {"section_heading": "Rule 2"},
    {"metadata": {"effective_date": "2025-01-01"}}, {"source": "other.pdf"},
])
def test_legally_distinct_evidence_survives(overrides):
    assert len(retrieval.diversify_contexts([context(), context(2, **overrides)], 2)) == 2


def test_containment_preserves_whole_clause_and_requires_provenance():
    assert retrieval.evidence_contains(context(), context(2, "Bamboo is exempt."))
    assert not retrieval.evidence_contains(context(section_heading=None), context(2, section_heading=None))
    assert not retrieval.evidence_contains(context(text="Fee 1000"), context(2, "Fee 100"))


def test_source_cap_fallback_still_excludes_duplicates():
    assert len(retrieval.diversify_contexts([context(), context(2)], 3, max_per_source=1)) == 1


def test_neighbor_exception_wins_last_slot_without_displacing_anchor():
    anchor = context()

    class Repository:
        def neighbor_chunks(self, *_args, **_kwargs):
            return [{"id": i, "document_id": "rules", "source": "rules.pdf", "chunk_index": i,
                     "chunk_type": "rule", "section_heading": "Rule 1", "page_start": 1, "page_end": 1,
                     "metadata": {}, "content": text}
                    for i, text in [(0, "General introduction to the department."),
                                    (2, "Except bamboo, which remains fee-exempt but requires approval.")]]

    selected = retrieval.expand_neighbors([anchor], [], Repository(), 2, enabled=True)
    assert selected[0] is anchor
    assert selected[1]["id"] == 2


def amendment():
    return context(text="Effective from 2024-01-01. This amendment sets the fee at INR 100.",
                   metadata={"effective_date": "2024-01-01", "amendment_references": ["Amends Rule 1"]})


def test_narrow_dated_amendment_uses_flash_medium(monkeypatch):
    assert prompts.answer_generation_operation("What is the fee in this amendment?", [amendment()]) == "answer_complex"
    monkeypatch.setenv("RAG_SELECTIVE_REASONING", "false")
    assert prompts.answer_generation_operation("What is the fee in this amendment?", [amendment()]) == "answer_complex_high"


@pytest.mark.parametrize("question", [
    "What is the fee after the amendment?", "What is the fee as of 2020?",
    "What is the current fee?", "Explain the conflicting fee schedules", "Which rule is in force?",
])
def test_temporal_reconciliation_keeps_high_reasoning(question):
    assert prompts.answer_generation_operation(question, [amendment()]) == "answer_complex_high"


def test_unknown_future_multiple_or_conflicting_evidence_keeps_high():
    original = amendment()
    for metadata in [{}, {"effective_date": "2099-01-01"},
                     {"effective_date": "2024-01-01", "amendment_references": ["Amends A", "Amends B"]}]:
        changed = deepcopy(original)
        changed["metadata"] = {"temporal_metadata_version": 1, "amendment_references": ["Amends A"], **metadata}
        assert prompts.answer_generation_operation("What is the fee?", [changed]) == "answer_complex_high"
    assert prompts.answer_generation_operation("What is the fee?", [original, context(2)]) == "answer_complex_high"
    assert prompts.answer_generation_operation("What is the fee?", [original], {"conflicts": ["Ambiguous rate"]}) == "answer_complex_high"


def test_repair_is_accepted_but_contradictory_approval_is_not():
    corrected = "Bamboo is fee-exempt but needs approval [1]."
    assert prompts.accepted_verification({"supported": True, "unchanged": False, "issues": ["Missing condition"],
                                         "answer": corrected}, "Bamboo is exempt [1].", compact=True) == corrected
    assert prompts.accepted_verification({"supported": True, "unchanged": True, "issues": ["Wrong fee"],
                                         "answer": ""}, "Fee 200 [1].", compact=True) is None
