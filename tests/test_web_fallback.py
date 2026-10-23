from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import chat_service
import web_fallback
from errors import AppError, ErrorCode
from prompts import INSUFFICIENT_EVIDENCE_ANSWER, UNSUPPORTED_ANSWER
from routers import qa
from schemas import AskRequest, AskResponse
from services.gemini import GeminiClient, GenerationResult


def grounded():
    return GenerationResult(
        "Unverified introduction. The fee is 100. Unsupported extra claim.", "model", "answer_complex", "STOP", {},
        {"webSearchQueries": ["permit fee"],
         "groundingChunks": [{"web": {"uri": "https://example.gov/fees", "title": "Official fees"}}],
         "groundingSupports": [{"segment": {"text": "The fee is 100."}, "groundingChunkIndices": [0]}],
         "searchEntryPoint": {"renderedContent": "<div>Search suggestions</div>"}},
    )


@pytest.fixture
def search(monkeypatch):
    monkeypatch.setenv("RAG_WEB_FALLBACK", "true")
    calls = []
    monkeypatch.setattr(web_fallback, "reserve_web_search", lambda *a: SimpleNamespace(settle=lambda result: None))

    def generate(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return grounded()

    monkeypatch.setattr(web_fallback.gemini_client, "generate_result", generate)
    return calls


def test_provider_enables_search_and_preserves_grounding(monkeypatch):
    client = GeminiClient()
    payloads = []

    def post(*args, **kwargs):
        payloads.append(args[2])
        return {"candidates": [{"content": {"parts": [{"text": "Fee"}]}, "finishReason": "STOP",
                                "groundingMetadata": {"webSearchQueries": ["fee"]}}]}

    monkeypatch.setattr(client, "_post", post)
    result = client.generate_result("Fee?", google_search=True)
    assert payloads[0]["tools"] == [{"google_search": {}}]
    assert result.grounding_metadata["webSearchQueries"] == ["fee"]
    client.generate_result("Fee?")
    assert "tools" not in payloads[1]


@pytest.mark.parametrize("answer", [INSUFFICIENT_EVIDENCE_ANSWER, UNSUPPORTED_ANSWER])
def test_ask_fallback_discloses_origin_and_separates_sources(monkeypatch, search, answer):
    monkeypatch.setattr(qa, "retrieve", lambda *a: [])
    monkeypatch.setattr(qa, "answer_with_gemini", lambda *a: answer)
    result = AskResponse.model_validate(qa._generate(AskRequest(question="What is the fee?")))
    assert result.answer_origin == "web" and not result.abstained
    assert result.outcome == "answered"
    assert result.answer.startswith(web_fallback.DISCLOSURE)
    assert "[Web 1](https://example.gov/fees)" in result.answer
    assert "Unsupported extra claim" not in result.answer
    assert not result.cited_sources and result.web_sources[0].title == "Official fees"
    assert result.web_search_entry_point
    assert search[0][1]["google_search"] is True
    assert search[0][1]["operation"] == "web_fallback"


def test_supported_answer_and_disabled_fallback_do_not_search(monkeypatch, search):
    result = {"answer": "Supported [1]"}
    assert web_fallback.apply_web_fallback(result, "Fee?") is result
    monkeypatch.setenv("RAG_WEB_FALLBACK", "false")
    result = {"answer": INSUFFICIENT_EVIDENCE_ANSWER}
    assert web_fallback.apply_web_fallback(result, "Fee?") is result
    assert not search


@pytest.mark.parametrize("defect", ["no_search", "no_support", "unsafe_url", "truncated", "invalid_index", "no_evidence", "non_primary"])
def test_search_without_usable_evidence_retains_abstention(monkeypatch, search, defect):
    result = grounded()
    metadata = result.grounding_metadata
    if defect == "no_search":
        metadata["webSearchQueries"] = []
    elif defect == "no_support":
        metadata["groundingSupports"] = []
    elif defect == "unsafe_url":
        metadata["groundingChunks"][0]["web"]["uri"] = "javascript:alert(1)"
    elif defect == "non_primary":
        metadata["groundingChunks"][0]["web"]["uri"] = "https://unofficial.example/fees"
    elif defect == "truncated":
        result = GenerationResult(result.text, result.model, result.operation, "MAX_TOKENS", {}, metadata)
    elif defect == "no_evidence":
        result = GenerationResult(web_fallback.NO_WEB_EVIDENCE, result.model, result.operation, "STOP", {}, metadata)
    else:
        metadata["groundingSupports"][0]["groundingChunkIndices"] = [-1, True, 50]
    monkeypatch.setattr(web_fallback.gemini_client, "generate_result", lambda *a, **kw: result)
    original = {"answer": INSUFFICIENT_EVIDENCE_ANSWER}
    assert web_fallback.apply_web_fallback(original, "Fee?") is original


def test_search_failure_retains_abstention(monkeypatch, search):
    def fail(*a, **kw):
        raise AppError("Unavailable", code=ErrorCode.UPSTREAM_ERROR)

    monkeypatch.setattr(web_fallback.gemini_client, "generate_result", fail)
    original = {"answer": INSUFFICIENT_EVIDENCE_ANSWER}
    assert web_fallback.apply_web_fallback(original, "Fee?") is original


@pytest.mark.parametrize("combined", [False, True])
def test_chat_fallback_completes_turn_with_web_provenance(monkeypatch, search, combined):
    monkeypatch.setenv("RAG_QUERY_CONTEXT_V2", str(combined).lower())
    monkeypatch.setenv("RAG_CONTEXT_RESOLVER", "combined" if combined else "legacy")
    context = SimpleNamespace(resolved_question="What is the permit fee?", messages=lambda: [],
                              policy_question=lambda: "What is the permit fee?")
    monkeypatch.setattr(chat_service, "resolve_combined", lambda *a: context)
    monkeypatch.setattr(chat_service, "use_context", lambda *a: nullcontext())
    monkeypatch.setattr(chat_service, "retrieve", lambda *a: [])
    monkeypatch.setattr(chat_service, "answer_with_gemini", lambda *a: INSUFFICIENT_EVIDENCE_ANSWER)
    repo = SimpleNamespace(
        begin_turn=lambda *a: {"state": "claimed"},
        turn_lease=lambda *a: nullcontext(SimpleNamespace(check=lambda: None)),
        get_recent_messages_for_answer=lambda *a: [],
        complete_turn=lambda *a: a[-1],
    )
    result = chat_service.chat_ask("session", "What is the permit fee?", "user", repository=repo)
    assert result["answer_origin"] == "web"
    assert len(search) == 1 and "permit fee" in search[0][0]
