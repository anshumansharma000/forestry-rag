import logging
from copy import deepcopy

import pytest

from errors import AppError, ErrorCode
from services.gemini import GeminiClient
from token_usage import track_query_usage


def response(reason="STOP", content=True):
    candidate = {"finishReason": reason}
    if content:
        candidate["content"] = {"parts": [{"text": "Complete answer [1]."}]}
    return {"candidates": [candidate], "usageMetadata": {
        "promptTokenCount": 100, "thoughtsTokenCount": 30, "candidatesTokenCount": 10, "totalTokenCount": 140}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GEMINI_TRUNCATION_RECOVERY", "true")
    monkeypatch.setenv("GEMINI_TRUNCATION_MAX_OUTPUT_TOKENS", "16384")
    monkeypatch.setenv("GEMINI_COMPLEX_HIGH_MAX_OUTPUT_TOKENS", "8192")
    monkeypatch.setenv("GEMINI_COMPLEX_HIGH_THINKING_LEVEL", "high")
    return GeminiClient()


@pytest.mark.parametrize("content", [True, False])
def test_retry_keeps_evidence_schema_model_and_thinking(client, monkeypatch, caplog, content):
    caplog.set_level(logging.INFO)
    calls = []

    def post(model, action, payload, **kwargs):
        calls.append((model, deepcopy(payload)))
        return response("MAX_TOKENS", content) if len(calls) == 1 else response()

    monkeypatch.setattr(client, "_post", post)

    @track_query_usage
    def query():
        return client.generate("Original evidence", operation="answer_complex_high",
                               system_instruction="Ground every claim", response_schema={"type": "object"})

    assert query() == "Complete answer [1]."
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0]
    assert calls[0][1]["generationConfig"]["maxOutputTokens"] == 8192
    assert calls[1][1]["generationConfig"]["maxOutputTokens"] == 16384
    calls[1][1]["generationConfig"]["maxOutputTokens"] = 8192
    assert calls[0] == calls[1]
    usage = next(r for r in caplog.records if r.message == "query_generation_usage")
    assert usage.generation_calls == 2
    assert usage.prompt_tokens == 200
    assert usage.thinking_tokens == 60
    assert usage.succeeded


def test_repeated_truncation_is_bounded_and_never_returns_partial(client, monkeypatch):
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        return response("MAX_TOKENS", False)

    monkeypatch.setattr(client, "_post", post)
    with pytest.raises(AppError) as error:
        client.generate("Evidence", operation="answer_complex_high")
    assert len(calls) == 2
    assert error.value.details["generation_attempts"] == 2
    assert error.value.details["max_output_tokens"] == 16384
    assert error.value.details["thinking_tokens"] == 30


@pytest.mark.parametrize("setting,value", [("GEMINI_TRUNCATION_RECOVERY", "false"),
                                          ("GEMINI_TRUNCATION_MAX_OUTPUT_TOKENS", "8192")])
def test_disabled_or_exhausted_recovery_does_not_repeat(client, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        return response("MAX_TOKENS")

    monkeypatch.setattr(client, "_post", post)
    with pytest.raises(AppError):
        client.generate("Evidence", operation="answer_complex_high")
    assert len(calls) == 1


def test_success_does_not_retry_and_medium_budget_unchanged(client, monkeypatch):
    calls = []
    monkeypatch.setenv("GEMINI_COMPLEX_MAX_OUTPUT_TOKENS", "4000")

    def post(model, action, payload, **kwargs):
        calls.append(deepcopy(payload))
        return response()

    monkeypatch.setattr(client, "_post", post)
    assert client.generate("Evidence", operation="answer_complex") == "Complete answer [1]."
    assert len(calls) == 1
    assert calls[0]["generationConfig"]["maxOutputTokens"] == 4000


def test_upstream_errors_do_not_trigger_truncation_recovery(client, monkeypatch):
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        raise AppError("Quota error", code=ErrorCode.UPSTREAM_ERROR)

    monkeypatch.setattr(client, "_post", post)
    with pytest.raises(AppError, match="Quota error"):
        client.generate("Evidence")
    assert len(calls) == 1
