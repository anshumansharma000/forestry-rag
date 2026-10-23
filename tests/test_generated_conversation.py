"""One bounded, tool-free routing call can also write an unfamiliar social reply."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import chat_service
import query_scope
import web_fallback
from prompts import INSUFFICIENT_EVIDENCE_ANSWER
from routers import qa
from schemas import AskRequest, AskResponse
from services.gemini import GeminiClient

MESSAGE = "You've been quite helpful today. Hope I'm not asking too many questions."
REPLY = "You're welcome to keep asking. What would you like to work through next?"


@pytest.fixture
def no_retrieval(monkeypatch):
    monkeypatch.setenv("RAG_SCOPE_GATE", "true")
    monkeypatch.setenv("RAG_WEB_FALLBACK", "true")

    def fail(*args, **kwargs):
        pytest.fail("Social replies must not reach retrieval, answer generation, caching, or search")

    for module in (qa, chat_service):
        monkeypatch.setattr(module, "retrieve", fail)
        monkeypatch.setattr(module, "answer_with_gemini", fail)
    monkeypatch.setattr(qa, "answer_cached", fail)
    monkeypatch.setattr(chat_service, "resolve_combined", fail)
    monkeypatch.setattr(chat_service, "rewrite_question_for_retrieval", fail)
    monkeypatch.setattr(web_fallback, "reserve_web_search", fail)


def test_unfamiliar_social_message_gets_reply_in_the_single_scope_call(monkeypatch, no_retrieval):
    seen = []

    def classify(prompt, schema, **kwargs):
        seen.append((json.loads(prompt), schema, kwargs))
        return {"scope": "conversational", "reply": REPLY}

    monkeypatch.setattr(query_scope.gemini_client, "generate_structured", classify)
    result = AskResponse.model_validate(qa.ask(AskRequest(question=MESSAGE), None))
    assert result.answer == REPLY
    assert result.outcome == result.answer_origin == "conversational"
    assert not result.abstained and result.confidence is None
    assert not result.sources and not result.cited_sources and not result.web_sources
    assert len(seen) == 1
    payload, schema, kwargs = seen[0]
    assert payload == {"latest_question": MESSAGE, "history": []}
    assert schema["required"] == ["scope", "reply"]
    assert kwargs == {"operation": "scope_classification", "parse_retries": 0,
                      "system_instruction": query_scope.SCOPE_INSTRUCTION}
    assert MESSAGE not in kwargs["system_instruction"]


@pytest.mark.parametrize("reply", [
    None, 42, {}, [], "", "   ", "x" * 321, "a " * 61,
    "Visit https://example.com", "Visit WWW.example.com", "<b>Hello</b>", "Hello [1]",
    "Hello\nSecond line", "Hello\x00", "Run `code`",
])
def test_invalid_generated_reply_uses_fixed_fallback_without_retry(monkeypatch, no_retrieval, reply):
    seen = []

    def classify(*args, **kwargs):
        seen.append(1)
        return {"scope": "conversational", "reply": reply}

    monkeypatch.setattr(query_scope.gemini_client, "generate_structured", classify)
    result = qa.ask(AskRequest(question=MESSAGE), None)
    assert result["answer"] == query_scope.CONVERSATIONAL_FALLBACK
    assert result["outcome"] == "conversational" and seen == [1]


@pytest.mark.parametrize("verdict", [None, [], {}, {"scope": "invalid", "reply": REPLY}])
def test_invalid_route_never_publishes_model_text(monkeypatch, no_retrieval, verdict):
    monkeypatch.setattr(query_scope.gemini_client, "generate_structured", lambda *a, **k: verdict)
    result = qa.ask(AskRequest(question=MESSAGE), None)
    assert result["outcome"] == "clarification_required"
    assert result["answer"] != REPLY and result["abstained"]


@pytest.mark.parametrize("scope", ["out_of_scope", "ambiguous", "in_scope"])
def test_model_reply_is_ignored_for_every_non_social_route(monkeypatch, scope):
    monkeypatch.setenv("RAG_SCOPE_GATE", "true")
    monkeypatch.setattr(query_scope.gemini_client, "generate_structured", lambda *a, **k: {"scope": scope, "reply": REPLY})
    result = query_scope.scope_response("Hello, explain the applicable rules and tell me the cricket score")
    if scope == "in_scope":
        assert result is None
    else:
        assert result["answer"] != REPLY and result["abstained"]


@pytest.mark.parametrize("combined", [False, True])
def test_generated_chat_reply_is_saved_and_replayed_without_regeneration(monkeypatch, no_retrieval, combined):
    monkeypatch.setenv("RAG_QUERY_CONTEXT_V2", str(combined).lower())
    monkeypatch.setenv("RAG_CONTEXT_RESOLVER", "combined" if combined else "legacy")
    history = [{"role": "assistant", "content": "Welcome!", "metadata": {"outcome": "conversational"}}]
    seen, completions, saved = [], [], {}

    def classify(prompt, *args, **kwargs):
        seen.append(json.loads(prompt))
        return {"scope": "conversational", "reply": REPLY}

    def complete(*args):
        completions.append(args[-1])
        saved.update(args[-1])
        return saved

    monkeypatch.setattr(query_scope.gemini_client, "generate_structured", classify)
    repo = SimpleNamespace(
        begin_turn=lambda *a: {"state": "completed", "response": saved} if saved else {"state": "claimed"},
        turn_lease=lambda *a: nullcontext(SimpleNamespace(check=lambda: None)),
        get_recent_messages_for_answer=lambda *a: history,
        complete_turn=complete,
    )
    first = chat_service.chat_ask("session", MESSAGE, "user", repository=repo, request_id="same")
    second = chat_service.chat_ask("session", MESSAGE, "user", repository=repo, request_id="same")
    assert first == second and first["answer"] == REPLY
    assert len(seen) == len(completions) == 1
    assert seen[0]["history"] == [{"role": "assistant", "content": "Welcome!"}]


@pytest.mark.parametrize("reply", ["आप बेझिझक पूछ सकते हैं। मैं मदद के लिए यहाँ हूँ।", "  Happy to help!  "])
def test_short_replies_preserve_language_and_strip_outer_whitespace(monkeypatch, reply):
    monkeypatch.setenv("RAG_SCOPE_GATE", "true")
    monkeypatch.setattr(query_scope.gemini_client, "generate_structured",
                        lambda *a, **k: {"scope": "conversational", "reply": reply})
    assert query_scope.scope_response(MESSAGE)["answer"] == reply.strip()


@pytest.mark.parametrize("mode", ["valid", "invalid_json", "truncated", "empty"])
def test_provider_call_is_tool_free_bounded_and_never_retried(monkeypatch, no_retrieval, mode):
    monkeypatch.setenv("GEMINI_SCOPE_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.delenv("GEMINI_SCOPE_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.setenv("GEMINI_SCOPE_THINKING_LEVEL", "minimal")
    monkeypatch.setenv("GEMINI_TRUNCATION_RECOVERY", "true")
    calls = []

    def post(model, action, payload, **kwargs):
        calls.append((model, payload, kwargs))
        text = json.dumps({"scope": "conversational", "reply": REPLY}) if mode == "valid" else "{invalid"
        candidate = {"finishReason": "MAX_TOKENS" if mode == "truncated" else "STOP",
                     "content": {"parts": [{"text": "" if mode == "empty" else text}]}}
        return {"candidates": [candidate]}

    client = GeminiClient()
    monkeypatch.setattr(client, "_post", post)
    monkeypatch.setattr(query_scope, "gemini_client", client)
    result = qa.ask(AskRequest(question=MESSAGE), None)
    assert result["outcome"] == ("conversational" if mode == "valid" else "clarification_required")
    assert len(calls) == 1
    model, payload, kwargs = calls[0]
    assert model == "gemini-3.5-flash-lite"
    assert "tools" not in payload
    assert payload["generationConfig"]["maxOutputTokens"] == 192
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert payload["generationConfig"]["responseJsonSchema"] == query_scope.SCOPE_SCHEMA
    assert kwargs["max_retries"] == 0


def test_direct_fallback_rechecks_scope_and_returns_social_reply_without_search(monkeypatch, no_retrieval):
    monkeypatch.setattr(query_scope.gemini_client, "generate_structured",
                        lambda *a, **k: {"scope": "conversational", "reply": REPLY})
    result = web_fallback.apply_web_fallback({"answer": INSUFFICIENT_EVIDENCE_ANSWER}, MESSAGE)
    assert result["outcome"] == "conversational" and result["answer"] == REPLY
