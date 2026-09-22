import logging

import pytest
import requests

import jev_policy
import prompts
import token_usage
from services import jev
from tests.test_lite_routing import source


def test_key_alias_and_protocol(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("JEV_KEY", "test-secret")
    class Response:
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def json(self):
            return {"model": "jev-1", "answers": {"a": {"type": "noul", "noul": .99}}, "usage": {"input_tokens": 100}}
    def post(url, **kwargs):
        assert url == jev.ENDPOINT
        assert kwargs["headers"]["Authorization"] == "Bearer test-secret"
        assert kwargs["allow_redirects"] is False
        assert kwargs["json"]["model"] == "jev-latest"
        return Response()
    monkeypatch.setattr(jev.requests, "post", post)
    assert jev.evaluate({}, {"a": {"type": "noul"}}, operation="test") == {"a": .99}


@pytest.mark.parametrize("value", [True, "0.99", float("nan"), float("inf"), -1, 1.01, None])
def test_invalid_probability_falls_back(monkeypatch, value):
    monkeypatch.setenv("JEV_KEY", "test-secret")
    class Response:
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def json(self):
            return {"answers": {"a": {"type": "noul", "noul": value}}}
    monkeypatch.setattr(jev.requests, "post", lambda *a, **kw: Response())
    assert jev.evaluate({}, {"a": {}}, operation="test") is None


def test_failure_is_metered_unknown_and_does_not_log_secrets(monkeypatch, caplog):
    monkeypatch.setenv("JEV_KEY", "test-secret")
    def fail(*args, **kwargs):
        raise requests.Timeout("test-secret confidential-source")
    monkeypatch.setattr(jev.requests, "post", fail)
    with caplog.at_level(logging.INFO):
        token_usage.track_query_usage(lambda: jev.evaluate({}, {"a": {}}, operation="test"))()
    record = next(r for r in caplog.records if r.msg == "query_generation_usage")
    assert record.decision_calls == 1
    assert record.total_api_cost_usd is None
    assert record.estimated_cost_usd == 0
    assert "test-secret" not in caplog.text and "confidential-source" not in caplog.text


def test_cost_added_without_changing_generation_fields(caplog):
    @token_usage.track_query_usage
    def query():
        token_usage.record_decision("test", "jev-1", {"input_tokens": 1000}, elapsed_ms=10, succeeded=True)
    with caplog.at_level(logging.INFO):
        query()
    record = next(r for r in caplog.records if r.msg == "query_generation_usage")
    assert record.total_api_cost_usd == pytest.approx(.000042)
    assert record.estimated_cost_usd == 0


@pytest.mark.parametrize("rollout,probability,expected,audits", [
    ("active", 1, "Submit Form A [1].", []),
    ("shadow", 1, "Submit Form A [1].", ["verify"]),
    ("off", 1, "Submit Form A [1].", ["verify"]),
    ("active", .98, prompts.UNSUPPORTED_ANSWER, []),
])
def test_verification_in_actual_answer_pipeline(monkeypatch, rollout, probability, expected, audits):
    monkeypatch.setenv("RAG_LITE_EXTRACTION", "true")
    monkeypatch.setenv("JEV_VERIFICATION_MODE", rollout)
    monkeypatch.setattr(jev_policy, "evaluate", lambda *a, **kw: dict.fromkeys(("support", "conditions", "scope"), probability))
    monkeypatch.setattr(prompts, "generate_with_gemini", lambda *a, **kw: "Submit Form A [1].")
    actual_audits = []
    def audit(*a, **kw):
        actual_audits.append(kw["operation"])
        return {"supported": True, "unchanged": True, "answer": "", "issues": []}
    monkeypatch.setattr(prompts, "generate_structured_with_gemini", audit)
    assert prompts.answer_with_gemini("List the application steps.", [source()]) == expected
    assert actual_audits == audits


@pytest.mark.parametrize("failure_policy,audits,expected", [
    ("closed", [], prompts.UNSUPPORTED_ANSWER),
    ("baseline", ["verify"], "Submit Form A [1]."),
])
def test_active_verification_failure_policy_is_configurable(monkeypatch, failure_policy, audits, expected):
    monkeypatch.setenv("RAG_LITE_EXTRACTION", "true")
    monkeypatch.setenv("JEV_VERIFICATION_MODE", "active")
    monkeypatch.setenv("JEV_ACTIVE_FAILURE_POLICY", failure_policy)
    monkeypatch.setattr(jev_policy, "evaluate", lambda *a, **kw: None)
    monkeypatch.setattr(prompts, "generate_with_gemini", lambda *a, **kw: "Submit Form A [1].")
    actual_audits = []
    def audit(*a, **kw):
        actual_audits.append(kw["operation"])
        return {"supported": True, "unchanged": True, "answer": "", "issues": []}
    monkeypatch.setattr(prompts, "generate_structured_with_gemini", audit)
    assert prompts.answer_with_gemini("List the application steps.", [source()]) == expected
    assert actual_audits == audits


@pytest.mark.parametrize("text", ["Bamboo is exempt from fees but requires approval.", "Subject to Rule 2, submit Form A."])
def test_legal_exclusions_never_call_jev(monkeypatch, text):
    monkeypatch.setenv("JEV_VERIFICATION_MODE", "active")
    monkeypatch.setattr(jev_policy, "evaluate", lambda *a, **kw: pytest.fail("Risky evidence sent to shortcut"))
    assert not jev_policy.approve_draft("List application steps.", "draft", [source(text)], text)


def test_high_risk_and_history_never_shortcut(monkeypatch):
    monkeypatch.setenv("JEV_VERIFICATION_MODE", "active")
    monkeypatch.setattr(jev_policy, "evaluate", lambda *a, **kw: pytest.fail("Risky shortcut"))
    for options in ({"high_risk": True}, {"history": True}):
        assert not jev_policy.approve_draft("List steps.", "draft", [source()], "text", **options)


def test_rerank_promotes_exception_preserving_scores_and_all_candidates(monkeypatch):
    monkeypatch.setenv("JEV_RERANK_MODE", "active")
    candidates = [{**source(), "id": str(i)} for i in range(15)]
    def evaluate(state, questions, **kwargs):
        assert len(state["candidates"]) == 12
        assert "candidates[2]" in questions["qualification_2"]["instructions"]
        return {k: 1 if k == "qualification_2" else .5 for k in questions}
    monkeypatch.setattr(jev_policy, "evaluate", evaluate)
    result = jev_policy.rerank("question", candidates)
    assert [c["id"] for c in result[:3]] == ["0", "2", "1"]
    assert sorted(c["id"] for c in result) == sorted(c["id"] for c in candidates)
    assert all(c["score"] == .9 for c in result)
    monkeypatch.setenv("JEV_RERANK_MODE", "shadow")
    assert jev_policy.rerank("question", candidates) is candidates
    monkeypatch.setenv("JEV_RERANK_MODE", "active")
    monkeypatch.setattr(jev_policy, "evaluate", lambda *a, **kw: None)
    assert jev_policy.rerank("question", candidates) is candidates


def test_rewrite_gate_requires_confident_self_containment(monkeypatch):
    monkeypatch.setenv("JEV_REWRITE_MODE", "active")
    messages = [{"role": "user", "content": "Only bamboo in Delhi, before 2020."}]
    monkeypatch.setattr(jev_policy, "evaluate", lambda *a, **kw: {"standalone": .5})
    assert not jev_policy.standalone_question(messages, "What fee?")
    monkeypatch.setattr(jev_policy, "evaluate", lambda *a, **kw: {"standalone": 1})
    assert jev_policy.standalone_question(messages, "What fee for timber in Mumbai in 2026?")
    monkeypatch.setenv("JEV_REWRITE_MODE", "shadow")
    assert not jev_policy.standalone_question(messages, "question")


def test_missing_key_and_oversized_request_make_no_network_call(monkeypatch):
    monkeypatch.delenv("JEV_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(jev.requests, "post", lambda *a, **kw: pytest.fail("Unexpected network request"))
    assert jev.evaluate({}, {}, operation="test") is None
    monkeypatch.setenv("JEV_KEY", "test-secret")
    assert jev.evaluate({"evidence": "x" * 60001}, {}, operation="test") is None


@pytest.mark.parametrize("body", [None, [], {}, {"answers": {}},
                                  {"answers": {"a": {"type": "choice", "choice": "yes"}}},
                                  {"answers": {"a": {"type": "noul", "noul": 1}, "extra": {}}}])
def test_malformed_response_falls_back(monkeypatch, body):
    monkeypatch.setenv("JEV_KEY", "test-secret")
    class Response:
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def json(self):
            return body
    monkeypatch.setattr(jev.requests, "post", lambda *a, **kw: Response())
    assert jev.evaluate({}, {"a": {}}, operation="test") is None


@pytest.mark.parametrize("status", [301, 401, 429, 500])
def test_http_failure_no_retry(monkeypatch, status):
    monkeypatch.setenv("JEV_KEY", "test-secret")
    calls = []
    class Response:
        status_code = status
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def json(self):
            pytest.fail("Error body should not be inspected")
    def post(*a, **kw):
        calls.append(1)
        return Response()
    monkeypatch.setattr(jev.requests, "post", post)
    assert jev.evaluate({}, {"a": {}}, operation="test") is None
    assert calls == [1]


def test_rerank_ignores_small_probability_differences(monkeypatch):
    monkeypatch.setenv("JEV_RERANK_MODE", "active")
    candidates = [{**source(), "id": str(i)} for i in range(3)]
    monkeypatch.setattr(jev_policy, "evaluate", lambda *a, **kw: {
        "relevant_0": .9, "qualification_0": .1, "relevant_1": .8,
        "qualification_1": .1, "relevant_2": .9, "qualification_2": .1})
    assert jev_policy.rerank("question", candidates) == candidates


def test_invalid_settings_visible_without_breaking_status(monkeypatch):
    from jev_settings import invalid_settings, status
    monkeypatch.setenv("JEV_VERIFICATION_MODE", "typo")
    monkeypatch.setenv("JEV_APPROVAL_THRESHOLD", "nan")
    monkeypatch.setenv("JEV_ACTIVE_FAILURE_POLICY", "retry-everything")
    monkeypatch.setenv("JEV_RAG_LAB_EXTRACTION_POLICY", "ignore")
    assert status()["jev_verification_mode"] == "typo"
    assert set(invalid_settings()) == {
        "JEV_VERIFICATION_MODE", "JEV_APPROVAL_THRESHOLD", "JEV_ACTIVE_FAILURE_POLICY",
        "JEV_RAG_LAB_EXTRACTION_POLICY",
    }


def test_chat_gate_retains_history_and_completes_inside_lease(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import chat_service

    history = [{"role": "user", "content": "earlier question"}]
    state = {"leased": False}
    @contextmanager
    def lease(*args):
        state["leased"] = True
        yield SimpleNamespace(check=lambda: None)
        state["leased"] = False
    def complete(*args):
        assert state["leased"]
        return args[-1]
    repo = SimpleNamespace(begin_turn=lambda *a: {"state": "claimed"}, turn_lease=lease, complete_turn=complete)
    monkeypatch.setattr(chat_service, "get_chat_messages", lambda *a, **kw: history)
    monkeypatch.setattr(jev_policy, "standalone_question", lambda *a: True)
    monkeypatch.setattr(chat_service, "rewrite_question_for_retrieval", lambda *a: pytest.fail("Unnecessary rewrite"))
    monkeypatch.setattr(chat_service, "retrieve", lambda query, k: [])
    def answer(question, contexts, answer_history):
        assert answer_history is history and state["leased"]
        return prompts.INSUFFICIENT_EVIDENCE_ANSWER
    monkeypatch.setattr(chat_service, "answer_with_gemini", answer)
    result = chat_service.chat_ask("session", " independent question ", "user", repository=repo)
    assert result["search_query"] == "independent question"


def test_active_history_does_not_run_gemini_history_selector(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import chat_service
    import jev_shadow

    history = [
        {"role": "user", "content": "Earlier constraint"},
        {"role": "assistant", "content": "Old answer"},
        {"role": "assistant", "content": "Latest answer"},
    ]
    @contextmanager
    def lease(*args):
        yield SimpleNamespace(check=lambda: None)
    repo = SimpleNamespace(
        begin_turn=lambda *a: {"state": "claimed"}, turn_lease=lease,
        complete_turn=lambda *args: args[-1],
    )
    monkeypatch.setenv("JEV_HISTORY_MODE", "active")
    monkeypatch.setattr(chat_service, "get_chat_messages", lambda *a, **kw: history)
    monkeypatch.setattr(jev_policy, "standalone_question", lambda *a: False)
    monkeypatch.setattr(chat_service, "select_history", lambda *a: pytest.fail("Gemini history selector ran"))
    monkeypatch.setattr(chat_service, "rewrite_question_for_retrieval", lambda *a: "resolved query")
    monkeypatch.setattr(jev_shadow, "history_selection", lambda *a: [history[0], history[2]])
    monkeypatch.setattr(chat_service, "retrieve", lambda *a: [])
    monkeypatch.setattr(chat_service, "answer_with_gemini", lambda q, c, h: prompts.INSUFFICIENT_EVIDENCE_ANSWER)
    result = chat_service.chat_ask("session", "follow-up", "user", repository=repo)
    assert result["search_query"] == "resolved query"


def test_standard_retrieval_uses_promoted_evidence(monkeypatch):
    import retrieval
    from tests.test_legal_hierarchy import Corpus, row

    monkeypatch.setenv("JEV_RERANK_MODE", "active")
    monkeypatch.setenv("RAG_LEGAL_HIERARCHY", "false")
    monkeypatch.setattr(retrieval, "embed_query", lambda _: [1.0])
    monkeypatch.setattr(jev_policy, "evaluate", lambda state, questions, **kw:
                        {key: .99 if key.endswith("_2") else .01 for key in questions})
    corpus = Corpus([row("anchor", score=.9), row("noise", score=.8), row("exception", score=.7)])
    contexts = retrieval.retrieve("Forest diversion", top_k=2, repository=corpus, options={"expand_neighbors": False})
    assert [ctx["id"] for ctx in contexts] == ["anchor", "exception"]


def test_legal_protected_anchor_is_never_sent_to_rerank(monkeypatch):
    import legal_retrieval
    from tests.test_legal_hierarchy import Corpus, Registry, row

    monkeypatch.setenv("JEV_RERANK_MODE", "active")
    monkeypatch.setattr(legal_retrieval, "embed_query", lambda _: [1.0])
    registry = Registry(
        {"h": {"instrument_type": "handbook", "reviewed": True},
         "j": {"instrument_type": "judicial", "reviewed": True}},
        {"handbook": [row("h", score=.99)], "judicial": [row("j", score=.4)]})
    def rerank(question, candidates):
        assert all(c["id"] not in {"h", "j"} for c in candidates)
        return list(reversed(candidates))
    monkeypatch.setattr(jev_policy, "rerank", rerank)
    contexts = legal_retrieval.retrieve_legal("Forest diversion", top_k=1,
                                            repository=Corpus([row("other")]), registry=registry)
    assert contexts[0]["document_id"] == "j"
