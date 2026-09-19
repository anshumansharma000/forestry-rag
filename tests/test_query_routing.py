import pytest

import prompts
import retrieval
from services.gemini import GENERATION_PROFILES


@pytest.mark.parametrize("question", [
    "How much is the application fee?", "How many days is the permit valid?",
    "How long does approval take?", "Who approves the application?",
    "Where do I apply for a permit?", "What is the application fee?",
    "Does this rule apply to bamboo?", "When does the permit expire?",
])
def test_factual_questions_do_not_trigger_procedural_pipeline(question):
    assert retrieval.classify_question_shape(question) == "direct"
    assert retrieval.retrieval_plan(question).shape == "direct"
    assert prompts.answer_generation_operation(question, []) == "answer_direct"


@pytest.mark.parametrize("question, expected", [
    ("How do I apply for a permit?", "procedure"),
    ("Help me apply for a permit", "procedure"),
    ("Apply for a permit", "procedure"),
    ("Guide me through applying for a permit", "procedure"),
    ("How much is the fee and how do I pay?", "procedure"),
    ("How many steps are in the application process?", "procedure"),
    ("What are the application requirements?", "overview"),
    ("What are the provisions for re-diversion?", "overview"),
    ("Compare the application procedures", "comparison"),
    ("How do the two procedures differ versus the older framework?", "comparison"),
])
def test_workflows_and_synthesis_retain_complex_pipeline(question, expected):
    assert retrieval.classify_question_shape(question) == expected
    assert prompts.answer_generation_operation(question, []) == "answer_complex"


@pytest.mark.parametrize("question", [
    "How much was the fee as of 2020?", "Which rule is in force?",
    "How much is the fee after the amendment?", "Explain the conflicting fee schedules",
])
def test_high_risk_questions_keep_high_reasoning(question):
    assert prompts.answer_generation_operation(question, []) == "answer_complex_high"


def test_background_document_count_does_not_escalate_simple_lookup():
    contexts = [{"document_id": str(i), "metadata": {}} for i in range(8)]
    assert prompts.answer_generation_operation("How much is the fee?", contexts) == "answer_direct"
    assert prompts.answer_generation_operation("Explain the relationship between these rules", contexts) == "answer_complex"
    contexts[0]["metadata"] = {"amendment_references": ["Amends the fee in Rule 12."]}
    assert prompts.answer_generation_operation("How much is the fee?", contexts) == "answer_complex_high"


def test_requested_model_split_is_preserved():
    assert GENERATION_PROFILES["answer_direct"].default_model == "gemini-3.5-flash-lite"
    assert GENERATION_PROFILES["answer_complex"].default_model == "gemini-3.8-flash"
    assert GENERATION_PROFILES["answer_complex_high"].default_thinking == "high"


def test_exact_repeated_evidence_is_aliased_without_losing_source_numbers():
    text = "Approval is mandatory except for bamboo. " * 30
    contexts = [
        {"source": "rules.pdf", "document_id": "one", "text": text, "page_start": page, "page_end": page}
        for page in [1, 2]
    ]
    result = prompts.format_contexts(contexts)
    assert result.count(text) == 1
    assert "[1] Source: rules.pdf, page 1" in result
    assert "[2] Source: rules.pdf, page 2" in result
    assert "Identical excerpt text to [1]" in result


@pytest.mark.parametrize("difference", [
    {"document_id": "other"}, {"source": "amendment.pdf"}, {"section_heading": "Other section"},
    {"metadata": {"effective_date": "2099-01-01", "temporal_metadata_version": 1}},
    {"text": "Approval is mandatory including for bamboo."},
])
def test_deduplication_preserves_different_provenance_dates_and_qualifications(difference):
    first = {"source": "rules.pdf", "document_id": "one", "text": "Approval is mandatory except for bamboo."}
    result = prompts.format_contexts([first, {**first, **difference}])
    assert "Identical excerpt" not in result


def test_concise_prompt_preserves_full_evidence_and_allows_requested_detail(monkeypatch):
    captured = []
    monkeypatch.setattr(prompts, "retrieval_is_confident", lambda contexts: True)
    monkeypatch.setattr(prompts, "generate_with_gemini", lambda p, **k: captured.append(p) or "Fee: 100 [1].")
    context = {"source": "rules.txt", "text": "The fee is 100 except for exempt bamboo permits."}
    prompts.answer_with_gemini("What is the fee?", [context])
    assert context["text"] in captured[0]
    assert "soft targets" in captured[0]
    assert "completeness, exceptions, or qualifications" in captured[0]
    prompts.answer_with_gemini("Explain the fee in detail", [context])
    assert "without an artificial word target" in captured[1]
    assert "under 120 words" not in captured[1]
