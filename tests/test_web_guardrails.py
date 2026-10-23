import logging
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
import requests

import chat_service
import query_scope
import web_fallback
import web_limits
import web_sources
from errors import AppError, ErrorCode
from generation_cost import generation_cost, normalized_usage, search_cost
from prompts import INSUFFICIENT_EVIDENCE_ANSWER
from routers import qa
from schemas import AskRequest
from services.gemini import GeminiClient
from token_usage import record_generation, record_search, record_unmetered_attempt, track_query_usage


@pytest.mark.parametrize("scope,outcome", [("out_of_scope", "out_of_scope"), ("ambiguous", "clarification_required"),
                                          ("invalid", "clarification_required")])
def test_scope_rejects_before_any_retrieval_or_search(monkeypatch, scope, outcome):
    monkeypatch.setenv("RAG_SCOPE_GATE", "true")
    monkeypatch.setattr(query_scope.gemini_client, "generate_structured", lambda *a, **k: {"scope": scope})
    monkeypatch.setattr(qa, "retrieve", lambda *a: pytest.fail("retrieval must not run"))
    monkeypatch.setattr(web_fallback, "reserve_web_search", lambda *a: pytest.fail("search must not run"))
    result = qa.ask(AskRequest(question="Who won the cricket match?"), None)
    assert result["outcome"] == outcome and result["abstained"]
    assert result["sources"] == []


def test_scope_accepts_relevant_queries_and_uses_tool_free_lite_profile(monkeypatch):
    monkeypatch.setenv("RAG_SCOPE_GATE", "true")
    seen = []

    def classify(prompt, schema, **kwargs):
        seen.append((prompt, kwargs))
        return {"scope": "in_scope"}

    monkeypatch.setattr(query_scope.gemini_client, "generate_structured", classify)
    history = [{"role": "user", "content": "Tell me about the timber transit permit"}]
    assert query_scope.scope_response("What is its fee?", history) is None
    assert "timber transit" in seen[0][0]
    assert seen[0][1]["operation"] == "scope_classification"
    assert seen[0][1]["parse_retries"] == 0
    assert "clear topic change" in seen[0][1]["system_instruction"]


def test_scope_history_has_a_bounded_input_budget():
    messages = [{"role": "user", "content": str(i) * 9000} for i in range(10)]
    selected = query_scope.scope_history(messages)
    assert sum(len(m["content"]) for m in selected) == 6000
    assert selected[-1]["content"].startswith("9")
    assert messages[-1]["content"] == "9" * 9000


def test_classifier_failure_requests_clarification_without_retrieval(monkeypatch):
    monkeypatch.setenv("RAG_SCOPE_GATE", "true")

    def fail(*a, **kw):
        raise AppError("Timeout", code=ErrorCode.UPSTREAM_ERROR)

    monkeypatch.setattr(query_scope.gemini_client, "generate_structured", fail)
    assert query_scope.scope_response("Which forest law applies?")["outcome"] == "clarification_required"


def test_chat_rejection_is_persisted_and_idempotent(monkeypatch):
    monkeypatch.setenv("RAG_SCOPE_GATE", "true")
    calls, saved = [], {}
    monkeypatch.setattr(query_scope.gemini_client, "generate_structured", lambda *a, **k: calls.append(1) or {"scope": "out_of_scope"})
    monkeypatch.setattr(chat_service, "retrieve", lambda *a: pytest.fail("retrieval must not run"))

    def complete(*a):
        saved.update(a[-1])
        return saved

    repo = SimpleNamespace(
        begin_turn=lambda *a: {"state": "completed", "response": saved} if saved else {"state": "claimed"},
        turn_lease=lambda *a: nullcontext(SimpleNamespace(check=lambda: None)),
        get_recent_messages_for_answer=lambda *a: [], complete_turn=complete,
    )
    for _ in range(2):
        result = chat_service.chat_ask("session", "Write me a cake recipe", "user", repository=repo, request_id="same")
        assert result["outcome"] == "out_of_scope"
    assert calls == [1]


def test_quota_denial_preserves_abstention_without_provider_call(monkeypatch):
    monkeypatch.setenv("RAG_WEB_FALLBACK", "true")
    monkeypatch.setattr(web_fallback, "reserve_web_search", lambda *a: None)
    monkeypatch.setattr(web_fallback.gemini_client, "generate_result", lambda *a, **k: pytest.fail("No quota"))
    original = {"answer": INSUFFICIENT_EVIDENCE_ANSWER}
    assert web_fallback.apply_web_fallback(original, "Forest permit fee", user_id="u") is original


def test_no_quota_store_or_identity_means_no_search(monkeypatch):
    monkeypatch.setattr(web_limits, "client", lambda: (_ for _ in ()).throw(RuntimeError("Redis down")))
    assert web_limits.reserve_web_search("user") is None
    assert web_limits.reserve_web_search(None) is None


def test_web_model_is_independent_and_never_retries_or_expands_tokens(monkeypatch):
    monkeypatch.setenv("GEMINI_COMPLEX_MODEL", "more-expensive-model")
    monkeypatch.setenv("GEMINI_WEB_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_TRUNCATION_RECOVERY", "true")
    calls = []

    def post(model, action, payload, **kwargs):
        calls.append((model, payload, kwargs))
        return {"candidates": [{"finishReason": "MAX_TOKENS", "groundingMetadata": {"webSearchQueries": ["forest fee"]}}]}

    client = GeminiClient()
    monkeypatch.setattr(client, "_post", post)
    with pytest.raises(AppError):
        client.generate_result("Forest fee?", operation="web_fallback", google_search=True)
    assert len(calls) == 1
    model, payload, kwargs = calls[0]
    assert model == "gemini-3.5-flash-lite"
    assert payload["generationConfig"]["maxOutputTokens"] == 1200
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert kwargs["max_retries"] == 0


@pytest.mark.parametrize("url", ["https://fakegov.in/rule", "https://gov.in.evil.example/rule",
                                  "http://moef.gov.in/rule", "https://moef.gov.in@evil.example/rule",
                                  "https://127.0.0.1/rule", "https://moef.gov.in:999/rule"])
def test_non_primary_or_unsafe_sources_never_get_fetched(monkeypatch, url):
    monkeypatch.setattr(web_sources.requests, "get", lambda *a, **k: pytest.fail("Must not fetch arbitrary URLs"))
    assert web_sources.primary_url(url) is None


def test_google_citation_redirect_is_checked_without_fetching_destination(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return nullcontext(SimpleNamespace(status_code=302, headers={"Location": "https://moef.gov.in/rule"}))

    monkeypatch.setattr(web_sources.requests, "get", get)
    assert web_sources.primary_url("https://vertexaisearch.cloud.google.com/grounding-api-redirect/id") == "https://moef.gov.in/rule"
    assert len(calls) == 1 and calls[0][1] == {"allow_redirects": False, "timeout": 5, "stream": True}


def test_unapproved_redirect_or_network_error_is_rejected(monkeypatch):
    url = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/id"
    monkeypatch.setattr(web_sources.requests, "get", lambda *a, **k: nullcontext(
        SimpleNamespace(status_code=302, headers={"Location": "http://127.0.0.1/private"})))
    assert web_sources.primary_url(url) is None

    def fail(*a, **k):
        raise requests.Timeout()

    monkeypatch.setattr(web_sources.requests, "get", fail)
    assert web_sources.primary_url(url) is None


def test_search_pricing_counts_unique_nonempty_queries_without_free_allowance():
    result = search_cost("gemini-3.5-flash-lite", {"webSearchQueries": ["forest fee", " forest fee ", "", "forest rules"]})
    assert result["search_queries"] == 2 and result["estimated_cost_usd"] == pytest.approx(.028)
    assert not result["free_allowance_applied"]
    assert search_cost("gemini-3.5-flash-lite", None)["estimated_cost_usd"] is None
    assert search_cost("unknown", {"webSearchQueries": ["fee"]})["estimated_cost_usd"] is None


def test_complete_token_and_search_costs_are_logged_together(caplog):
    usage = {"promptTokenCount": 1000, "toolUsePromptTokenCount": 200,
             "candidatesTokenCount": 300, "totalTokenCount": 1600}
    assert normalized_usage(usage)["thoughtsTokenCount"] == 100
    expected = (1200 * .30 + 400 * 2.50) / 1_000_000
    assert generation_cost("gemini-3.5-flash-lite", usage)["estimated_cost_usd"] == pytest.approx(expected)

    @track_query_usage
    def answer():
        record_generation("web_fallback", "gemini-3.5-flash-lite", usage)
        record_search("gemini-3.5-flash-lite", {"webSearchQueries": ["forest permit fee"]})
        return {"outcome": "answered"}

    with caplog.at_level(logging.INFO, logger="token_usage"):
        answer()
    record = [r for r in caplog.records if r.message == "query_answer_cost"][0]
    assert record.total_api_cost_complete
    assert record.total_api_cost_usd == pytest.approx(expected + .014)
    assert record.search_queries == 1


def test_missing_search_metadata_stays_unpriced(caplog):
    @track_query_usage
    def answer():
        record_search("gemini-3.5-flash-lite", None)

    with caplog.at_level(logging.INFO, logger="token_usage"):
        answer()
    record = [r for r in caplog.records if r.message == "query_answer_cost"][0]
    assert record.total_api_cost_usd is None and record.search_queries is None


def test_failed_search_with_unknown_usage_does_not_log_zero_queries(caplog):
    @track_query_usage
    def answer():
        record_unmetered_attempt("web_fallback", "gemini-3.5-flash-lite")

    with caplog.at_level(logging.INFO, logger="token_usage"):
        answer()
    record = [r for r in caplog.records if r.message == "query_answer_cost"][0]
    assert record.total_api_cost_usd is None and record.search_queries is None
    assert record.search_cost_usd is None
