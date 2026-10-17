import logging

import pytest

from token_usage import record_embedding, record_generation, record_unmetered_attempt, track_query_usage


def generation():
    record_generation(
        "answer_direct",
        "gemini-3.5-flash-lite",
        {
            "promptTokenCount": 1000,
            "candidatesTokenCount": 100,
            "thoughtsTokenCount": 0,
        },
    )


def completion(caplog):
    return [record for record in caplog.records if record.message == "query_answer_cost"]


def test_cost_logged_after_completion_once_for_nested_calls(caplog, monkeypatch):
    monkeypatch.setenv("COST_USD_TO_INR", "90")

    @track_query_usage
    def inner():
        generation()

    @track_query_usage
    def answer():
        inner()
        assert not completion(caplog)
        return {"outcome": "answered"}

    with caplog.at_level(logging.INFO, logger="token_usage"):
        answer()
    records = completion(caplog)
    assert len(records) == 1
    result = records[0]
    assert result.total_api_cost_usd == pytest.approx(0.00055)
    assert result.total_api_cost_inr == pytest.approx(0.0495)
    assert result.total_api_cost_complete
    assert result.query_outcome == "answered"
    assert result.generation_models == ["gemini-3.5-flash-lite"]
    assert "USD 0.000550" in result.cost_summary


def test_unknown_embedding_cost_preserves_known_subtotal(caplog):
    @track_query_usage
    def answer():
        generation()
        record_embedding("unpriced-embedding", {"promptTokenCount": 100})

    with caplog.at_level(logging.INFO, logger="token_usage"):
        answer()
    result = completion(caplog)[0]
    assert result.total_api_cost_usd is None
    assert not result.total_api_cost_complete
    assert result.known_api_cost_usd == pytest.approx(0.00055)
    assert result.unpriced_calls == 1
    assert "incomplete" in result.cost_summary


def test_failed_query_logs_unknown_cost_and_propagates_error(caplog):
    @track_query_usage
    def answer():
        record_unmetered_attempt()
        raise RuntimeError("failure")

    with caplog.at_level(logging.INFO, logger="token_usage"), pytest.raises(RuntimeError):
        answer()
    result = completion(caplog)[0]
    assert not result.succeeded
    assert result.query_outcome == "failed"
    assert result.total_api_cost_usd is None
    assert result.unmetered_generation_attempts == 1


def test_no_upstream_calls_log_zero_request_cost(caplog):
    @track_query_usage
    def cached_answer():
        return {"outcome": "answered"}

    with caplog.at_level(logging.INFO, logger="token_usage"):
        cached_answer()
    result = completion(caplog)[0]
    assert result.total_api_cost_usd == 0
    assert result.total_api_cost_complete
    assert result.generation_calls == 0
