import logging
from datetime import date

import pytest

import conversation_context as conversation
import prompts
from generation_cost import generation_cost
from token_usage import record_generation, record_unmetered_attempt, track_query_usage


def context(source="rules.pdf", **kwargs):
    return {"source": source, "text": "Submit Form A; bamboo is fee-exempt but still needs approval.", **kwargs}


def test_small_procedure_skips_only_planning(monkeypatch):
    monkeypatch.setenv("RAG_COST_OPTIMIZATIONS", "true")
    monkeypatch.setenv("RAG_SELECTIVE_PLANNING", "true")
    monkeypatch.setenv("RAG_EVIDENCE_PLANNING", "true")
    monkeypatch.setenv("RAG_ANSWER_VERIFICATION", "true")
    monkeypatch.setattr(prompts, "retrieval_is_confident", lambda contexts: True)
    operations = []

    def generate(prompt, **kwargs):
        operations.append(kwargs["operation"])
        assert "bamboo is fee-exempt but still needs approval" in prompt
        return "Submit Form A [1]."

    def audit(prompt, schema, **kwargs):
        operations.append(kwargs["operation"])
        assert "bamboo is fee-exempt but still needs approval" in prompt
        return {"supported": True, "unchanged": True, "answer": "", "issues": []}

    monkeypatch.setattr(prompts, "generate_with_gemini", generate)
    monkeypatch.setattr(prompts, "generate_structured_with_gemini", audit)
    assert prompts.answer_with_gemini("How do I apply?", [context()]) == "Submit Form A [1]."
    assert operations == ["answer_complex", "verify"]
    monkeypatch.setenv("RAG_COST_OPTIMIZATIONS", "false")
    assert prompts.needs_evidence_plan("How do I apply?", [context()])


@pytest.mark.parametrize("question, contexts", [
    ("How do I apply?", [context("one"), context("two")]),
    ("How do I apply after the amendment?", [context()]),
    ("How do I apply?", [context(metadata={"amendment_references": ["Amends Rule 12"]})]),
    ("How do I apply?", [context(text="legal provision " * 3000)]),
    ("Explain the application process in detail", [context()]),
    ("Compare application procedures", [context()]),
    ("What are the provisions?", [context()]),
])
def test_complex_planning_is_retained(question, contexts):
    assert prompts.needs_evidence_plan(question, contexts)


def history():
    return [
        {"role": "user", "content": "Use Maharashtra rules as of 2020. Exclude bamboo; answer in Hindi."},
        {"role": "assistant", "content": "Earlier numbered options: 1. timber 2. bamboo"},
        {"role": "user", "content": "Correction: use 2021, not 2020. Tell me about nurseries too."},
        {"role": "assistant", "content": "Unrelated nursery details. " * 1000},
        {"role": "user", "content": "Back to the first option."},
        {"role": "assistant", "content": "You selected timber. The date remains 2021."},
    ]


def test_history_selection_preserves_verbatim_constraints_and_referenced_replies(monkeypatch):
    original = history()
    monkeypatch.setattr(conversation, "generate_structured_with_gemini", lambda *a, **k: {
        "query": "Maharashtra timber permit as of 2021 excluding bamboo",
        "can_reduce_history": True, "assistant_indices": [1],
    })
    query, selected = conversation.select_history(original, "What do I need for that option?")
    assert "2021" in query
    assert selected == [original[i] for i in [0, 1, 2, 4, 5]]
    assert selected[0] is original[0]
    assert original == history()  # no mutation or persisted summary drift


@pytest.mark.parametrize("indices, can_reduce", [([100], True), ([0], True), ([True], True), ([], False), (None, True)])
def test_uncertain_or_invalid_selection_keeps_entire_recent_history(monkeypatch, indices, can_reduce):
    monkeypatch.setattr(conversation, "generate_structured_with_gemini", lambda *a, **k: {
        "query": "Valid standalone query", "can_reduce_history": can_reduce, "assistant_indices": indices,
    })
    assert conversation.select_history(history(), "That one?")[1] == history()


def test_malformed_query_falls_back_to_legacy_rewrite(monkeypatch):
    monkeypatch.setattr(conversation, "generate_structured_with_gemini", lambda *a, **k: {"query": " "})
    monkeypatch.setattr(conversation, "rewrite_question_for_retrieval", lambda *a: "Legacy resolved query")
    assert conversation.select_history(history(), "That one?") == ("Legacy resolved query", history())


def test_history_selection_does_not_add_a_call_for_short_history_or_baseline(monkeypatch):
    assert not conversation.should_select_history(history()[:1])
    monkeypatch.setenv("RAG_HISTORY_SELECTION_TOKENS", "100")
    assert conversation.should_select_history(history())
    monkeypatch.setenv("RAG_COST_OPTIMIZATIONS", "false")
    assert not conversation.should_select_history(history())


def usage(**changes):
    return {"promptTokenCount": 10000, "candidatesTokenCount": 2000, "thoughtsTokenCount": 1000,
            "cachedContentTokenCount": 4000, "totalTokenCount": 13000, **changes}


def test_cost_counts_thinking_and_discounts_cached_input_once(monkeypatch):
    monkeypatch.setenv("COST_USD_TO_INR", "90")
    result = generation_cost("gemini-3.8-flash", usage(), as_of=date(2026, 9, 17))
    expected = (6000 * .75 + 4000 * .075 + 3000 * 3.75) / 1_000_000
    assert result["estimated_cost_usd"] == pytest.approx(expected)
    assert result["estimated_cost_inr"] == pytest.approx(expected * 90)
    future = generation_cost("gemini-3.8-flash", usage(), as_of=date(2027, 1, 1))
    assert future["estimated_cost_usd"] == pytest.approx(2 * expected)


def test_lite_cost_can_infer_thinking_from_complete_total():
    data = usage()
    del data["thoughtsTokenCount"]
    result = generation_cost("gemini-3.5-flash-lite", data)
    assert result["estimated_cost_usd"] == pytest.approx((6000*.3 + 4000*.03 + 3000*2.5) / 1_000_000)


@pytest.mark.parametrize("data", [
    {}, usage(promptTokenCount=None), usage(cachedContentTokenCount=20000),
    usage(serviceTier="priority"), usage(thoughtsTokenCount=None, totalTokenCount=None),
    usage(toolUsePromptTokenCount=100),
])
def test_incomplete_or_unsupported_cost_is_unknown(data):
    assert generation_cost("gemini-3.8-flash", data)["estimated_cost_usd"] is None
    assert generation_cost("unknown-model", usage())["estimated_cost_usd"] is None


def test_invalid_fx_does_not_break_generation_or_invent_inr_cost(monkeypatch):
    monkeypatch.setenv("COST_USD_TO_INR", "nan")
    result = generation_cost("gemini-3.8-flash", usage())
    assert result["estimated_cost_usd"] is not None
    assert result["estimated_cost_inr"] is None


def test_clean_audit_that_echoes_exact_draft_does_not_need_costly_escalation():
    draft = "Fee is 100, except bamboo [1]."
    result = {"supported": True, "unchanged": True, "answer": draft, "issues": []}
    assert prompts.accepted_verification(result, draft, compact=True) == draft
    result["answer"] = "Fee is 100 for everyone [1]."
    assert prompts.accepted_verification(result, draft, compact=True) is None


def test_query_cost_sums_each_model_and_reports_threshold_without_aborting(monkeypatch, caplog):
    monkeypatch.setenv("COST_USD_TO_INR", "90")
    monkeypatch.setenv("COST_QUERY_MAX_INR", "0.01")
    caplog.set_level(logging.INFO)

    @track_query_usage
    def query():
        record_generation("plan", "gemini-3.5-flash-lite", usage())
        record_generation("answer_complex", "gemini-3.8-flash", usage())
        return "Complete answer"

    assert query() == "Complete answer"
    record = next(r for r in caplog.records if r.message == "query_generation_usage")
    expected = sum(generation_cost(model, usage())["estimated_cost_inr"]
                   for model in ["gemini-3.5-flash-lite", "gemini-3.8-flash"])
    assert record.estimated_cost_inr == pytest.approx(expected)
    assert record.cost_exceeded is True
    assert record.cost_complete is True


def test_unmetered_attempt_prevents_false_complete_cost(caplog):
    caplog.set_level(logging.INFO)

    @track_query_usage
    def query():
        record_unmetered_attempt()
        record_generation("answer_direct", "gemini-3.5-flash-lite", usage())

    query()
    record = next(r for r in caplog.records if r.message == "query_generation_usage")
    assert record.estimated_cost_usd is None
    assert record.cost_complete is False
    assert record.unmetered_generation_attempts == 1
