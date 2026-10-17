"""Lite-first routing keeps difficult legal reconciliation on Flash."""

import pytest

import jev_shadow
import prompts
from model_routing import lite_first_enabled, routine_lite_eligible
from tests.test_lite_routing import source


@pytest.fixture(autouse=True)
def lite_first(monkeypatch):
    monkeypatch.setenv("RAG_LITE_FIRST", "true")
    monkeypatch.setenv("RAG_COST_OPTIMIZATIONS", "true")


@pytest.mark.parametrize(
    "question",
    [
        "How do I apply for a permit?",
        "What are the application requirements?",
        "Summarize the permit procedure.",
        "List the steps and fees.",
        "What is the fee?",
    ],
)
def test_routine_questions_use_lite(question):
    rows = [source("Submit Form A. The fee is INR 100 except for bamboo permits.")]
    assert prompts.answer_generation_operation(question, rows) == "answer_direct"
    assert not prompts.needs_evidence_plan(question, rows)


@pytest.mark.parametrize(
    "question",
    [
        "Which rule is in force?",
        "Does this apply to my case?",
        "What was the fee as of 2020?",
        "Compare the two procedures",
        "Explain the conflicting provisions",
        "Summarize the court judgment",
        "Give a comprehensive overview of the requirements",
    ],
)
def test_hard_questions_retain_flash(question):
    assert not routine_lite_eligible(question, [source()])
    assert prompts.answer_generation_operation(question, [source()]) != "answer_direct"


@pytest.mark.parametrize("plan", [{"conflicts": ["fees differ"]}, {"unknowns": ["effective date"]}])
def test_plan_uncertainty_retains_flash(plan):
    assert not routine_lite_eligible("List the steps", [source()], plan)


def test_review_gaps_and_large_evidence_do_not_downgrade():
    row = source()
    row["metadata"] = {"legal_review": {"missing_relationship_count": 1}}
    assert not routine_lite_eligible("List the steps", [row])
    assert not routine_lite_eligible("List the steps", [source()] * 11)
    assert not routine_lite_eligible("List the steps", [source("word " * 7000)])
    assert not routine_lite_eligible("List the steps", [source("Subject to Rule 16, submit Form A.")])


def test_jev_synthesis_alone_cannot_promote_but_legal_risk_can(monkeypatch):
    monkeypatch.setattr(jev_shadow, "enabled", lambda feature: "active")
    kwargs = {"evidence_plan": None, "history": False}
    assert jev_shadow.apply_route({"route": "answer_complex"}, "answer_direct", "List the steps", [source()], **kwargs) == "answer_direct"
    assert (
        jev_shadow.apply_route({"route": "answer_complex_high"}, "answer_direct", "List the steps", [source()], **kwargs)
        == "answer_complex_high"
    )


def test_new_lite_route_is_audited_even_if_general_audit_off(monkeypatch):
    monkeypatch.setenv("RAG_ANSWER_VERIFICATION", "false")
    monkeypatch.setattr(prompts, "retrieval_is_confident", lambda rows: True)
    calls = []
    monkeypatch.setattr(prompts, "generate_with_gemini", lambda *a, **kw: calls.append(kw["operation"]) or "Submit Form A [1].")
    monkeypatch.setattr(
        prompts,
        "generate_structured_with_gemini",
        lambda *a, **kw: calls.append(kw["operation"])
        or {
            "supported": True,
            "unchanged": True,
            "answer": "",
            "issues": [],
        },
    )
    assert prompts.answer_with_gemini("What are the application requirements?", [source()]) == "Submit Form A [1]."
    assert calls == ["answer_direct", "verify"]


def test_default_and_explicit_rollback(monkeypatch):
    monkeypatch.delenv("RAG_LITE_FIRST")
    assert lite_first_enabled()
    monkeypatch.setenv("RAG_LITE_FIRST", "false")
    assert prompts.answer_generation_operation("What are the application requirements?", [source()]) == "answer_complex"
