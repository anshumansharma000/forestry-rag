import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Barrier
from types import SimpleNamespace

import pytest

import chat_service
import prompts
from chunking import count_tokens
from errors import AppError
from services.gemini import GeminiClient
from token_usage import record_generation, track_query_usage


def test_first_message_skips_rewrite_without_changing_identifiers(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("First message must not incur a rewrite call")

    monkeypatch.setattr(prompts, "generate_with_gemini", unexpected)
    question = "Rule 12, dated 1 July 2023: what is the fee?"
    assert prompts.rewrite_question_for_retrieval([], question) == question


def test_followup_still_uses_full_recent_history(monkeypatch):
    history = [{"role": "assistant", "content": "Rule 12 applies to bamboo; Rule 13 applies to teak."}]
    captured = []
    monkeypatch.setattr(
        prompts, "generate_with_gemini",
        lambda prompt, **kwargs: captured.append(prompt) or "What is the teak fee under Rule 13?",
    )
    assert "Rule 13" in prompts.rewrite_question_for_retrieval(history, "What about the latter?")
    assert history[0]["content"] in captured[0]


def test_chat_passes_prior_history_without_duplicating_latest_question(monkeypatch):
    history = [{"role": "user", "content": "Explain Rule 12"}]
    monkeypatch.setattr(chat_service, "get_chat_messages", lambda *a, **k: history)
    monkeypatch.setattr(chat_service, "save_chat_message", lambda sid, role, content, **k: {"role": role, "content": content})
    monkeypatch.setattr(chat_service, "rewrite_question_for_retrieval", lambda *a: "Rule 12 fee")
    monkeypatch.setattr(chat_service, "retrieve", lambda *a: [])
    captured = []
    monkeypatch.setattr(chat_service, "answer_with_gemini", lambda q, c, h: captured.append((q, h)) or "No evidence")
    chat_service.chat_ask("session", "What is the fee?", "user", repository=SimpleNamespace(
        begin_turn=lambda *a: {"state": "claimed"},
        turn_lease=lambda *a: nullcontext(SimpleNamespace(check=lambda: None)),
        complete_turn=lambda *a: a[-1],
    ))
    assert captured == [("What is the fee?", history)]


@pytest.mark.parametrize("compact", [True, False])
def test_verifier_preserves_evidence_and_can_correct_draft(monkeypatch, compact):
    monkeypatch.setenv("RAG_COMPACT_VERIFICATION", str(compact))
    source = "Fee is 100. Exemption applies to bamboo. Effective 1 July 2023."
    corrected = "From 1 July 2023 the fee is 100, with an exemption for bamboo [1]."
    captured = []
    result = {"supported": True, "unchanged": False, "answer": corrected, "issues": ["Wrong fee"]}
    monkeypatch.setattr(
        prompts, "generate_structured_with_gemini",
        lambda prompt, schema, **kwargs: captured.append(prompt) or result,
    )
    assert prompts.verify_answer_with_gemini("What fee applies?", "Fee is 200 [1].", source) == corrected
    assert source in captured[0]
    assert "Fee is 200 [1]." in captured[0]
    assert "Check dates" in captured[0]


def test_compact_verification_returns_original_draft_exactly(monkeypatch):
    monkeypatch.setenv("RAG_COMPACT_VERIFICATION", "true")
    result = {"supported": True, "unchanged": True, "answer": "", "issues": []}
    monkeypatch.setattr(prompts, "generate_structured_with_gemini", lambda *a, **k: result)
    draft = "Approval is required [1].\n\nConditions apply [2].\n\nIf useful, I can explain the procedure."
    assert prompts.verify_answer_with_gemini("Explain", draft, "Evidence") == draft
    legacy = {"supported": True, "answer": draft, "issues": []}
    assert count_tokens(json.dumps(result)) < count_tokens(json.dumps(legacy))


@pytest.mark.parametrize("result", [
    {"supported": False, "unchanged": True, "answer": "", "issues": []},
    {"supported": True, "unchanged": True, "answer": "", "issues": ["Unsupported date"]},
    {"supported": True, "unchanged": True, "answer": "Different answer", "issues": []},
    {"supported": True, "unchanged": "true", "answer": "", "issues": []},
    {"supported": True, "unchanged": False, "answer": "", "issues": []},
    {"supported": True, "answer": "", "issues": []},
])
def test_invalid_or_unsupported_verification_never_passes_draft(monkeypatch, result):
    monkeypatch.setenv("RAG_COMPACT_VERIFICATION", "true")
    monkeypatch.setattr(prompts, "generate_structured_with_gemini", lambda *a, **k: result)
    assert prompts.verify_answer_with_gemini("Fee?", "Invented fee [1]", "Evidence") == prompts.UNSUPPORTED_ANSWER


def test_verification_failure_still_abstains(monkeypatch):
    def fail(*args, **kwargs):
        raise AppError("Upstream failure", code="upstream_error")

    monkeypatch.setattr(prompts, "generate_structured_with_gemini", fail)
    assert prompts.verify_answer_with_gemini("Fee?", "Draft", "Evidence") == prompts.UNSUPPORTED_ANSWER


def test_rejected_lite_audit_escalates_once_without_bypassing_verification(monkeypatch):
    monkeypatch.setenv("RAG_COMPACT_VERIFICATION", "true")
    monkeypatch.setenv("RAG_VERIFICATION_ESCALATION", "true")
    operations = []

    def audit(prompt, schema, **kwargs):
        operations.append(kwargs["operation"])
        assert "Full evidence" in prompt
        if kwargs["operation"] == "verify":
            return {"supported": False, "unchanged": False, "answer": prompts.UNSUPPORTED_ANSWER, "issues": []}
        return {"supported": True, "unchanged": True, "answer": "", "issues": []}

    monkeypatch.setattr(prompts, "generate_structured_with_gemini", audit)
    assert prompts.verify_answer_with_gemini("Fee?", "Fee is 100 [1].", "Full evidence") == "Fee is 100 [1]."
    assert operations == ["verify", "verify_complex"]


def test_verification_escalation_is_bounded_and_can_be_disabled(monkeypatch):
    monkeypatch.setenv("RAG_COMPACT_VERIFICATION", "true")
    operations = []

    def reject(*args, **kwargs):
        operations.append(kwargs["operation"])
        return {"supported": False, "unchanged": False, "answer": prompts.UNSUPPORTED_ANSWER, "issues": []}

    monkeypatch.setattr(prompts, "generate_structured_with_gemini", reject)
    monkeypatch.setenv("RAG_VERIFICATION_ESCALATION", "true")
    assert prompts.verify_answer_with_gemini("Fee?", "Draft", "Evidence") == prompts.UNSUPPORTED_ANSWER
    assert operations == ["verify", "verify_complex"]
    operations.clear()
    monkeypatch.setenv("RAG_VERIFICATION_ESCALATION", "false")
    assert prompts.verify_answer_with_gemini("Fee?", "Draft", "Evidence") == prompts.UNSUPPORTED_ANSWER
    assert operations == ["verify"]


def test_usage_includes_truncated_generations_and_resets_between_queries(monkeypatch, caplog):
    monkeypatch.setenv("GEMINI_TRUNCATION_RECOVERY", "false")
    caplog.set_level(logging.INFO)
    client = GeminiClient()
    metadata = {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 5,
                "cachedContentTokenCount": 50, "totalTokenCount": 125}
    monkeypatch.setattr(client, "_post", lambda *a, **k: {
        "candidates": [{"content": {"parts": [{"text": "partial"}]}, "finishReason": "MAX_TOKENS"}],
        "usageMetadata": metadata,
    })

    @track_query_usage
    def query():
        record_generation("plan", "model", metadata)
        return client.generate("private prompt")

    with pytest.raises(AppError):
        query()
    record = next(r for r in caplog.records if r.message == "query_generation_usage")
    assert record.prompt_tokens == 200
    assert record.output_tokens == 40
    assert record.thinking_tokens == 10
    assert record.cached_tokens == 100
    assert record.total_tokens == 250
    assert record.generation_calls == 2
    assert record.succeeded is False
    assert record_generation("answer", "model", metadata) is None
    assert "private prompt" not in caplog.text


def test_missing_usage_is_unknown_not_zero(caplog):
    caplog.set_level(logging.INFO)

    @track_query_usage
    def query():
        record_generation("answer", "model", {})

    query()
    record = next(r for r in caplog.records if r.message == "query_generation_usage")
    assert record.prompt_tokens is None
    assert record.total_tokens is None


def test_concurrent_queries_do_not_mix_usage(caplog):
    caplog.set_level(logging.INFO)
    barrier = Barrier(2)

    @track_query_usage
    def query(tokens):
        record_generation("answer", "model", {"promptTokenCount": tokens})
        barrier.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(query, [100, 300]))
    records = [r for r in caplog.records if r.message == "query_generation_usage"]
    assert sorted(r.prompt_tokens for r in records) == [100, 300]
    assert len({r.query_id for r in records}) == 2


def test_compact_approval_still_runs_planning_and_final_citation_validation(monkeypatch):
    monkeypatch.setenv("RAG_COMPACT_VERIFICATION", "true")
    monkeypatch.setenv("RAG_EVIDENCE_PLANNING", "true")
    monkeypatch.setenv("RAG_ANSWER_VERIFICATION", "true")
    monkeypatch.setattr(prompts, "retrieval_is_confident", lambda contexts: True)
    calls = []
    evidence = "Prior approval is required; bamboo is exempt."

    def structured(prompt, schema, **kwargs):
        calls.append(kwargs["operation"])
        assert evidence in prompt
        if kwargs["operation"] == "plan":
            return {"central_answer": "Prior approval is required, except for bamboo.", "conflicts": []}
        return {"supported": True, "unchanged": True, "answer": "", "issues": []}

    monkeypatch.setattr(prompts, "generate_structured_with_gemini", structured)
    draft = "Prior approval is required; bamboo is exempt [1]."
    monkeypatch.setattr(prompts, "generate_with_gemini", lambda *a, **k: draft)
    contexts = [{"source": "rules.pdf", "text": evidence, "chunk_index": 0}]
    assert prompts.answer_with_gemini("What are the provisions?", contexts) == draft
    assert calls == ["plan", "verify"]
    calls.clear()
    monkeypatch.setattr(prompts, "generate_with_gemini", lambda *a, **k: "Invented provision [99].")
    assert prompts.answer_with_gemini("What are the provisions?", contexts) == prompts.UNSUPPORTED_ANSWER
    assert calls == ["plan"]
