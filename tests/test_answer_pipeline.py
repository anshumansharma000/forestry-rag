"""Regression boundaries for the six-priority implementation; no paid calls."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
import redis

import decision_policy
import jev_shadow
import pipeline_network
import prompts
import query_cache
import retrieval
from evidence_policy import coherent_synthesis, diagnostics, material_amendment
from extraction_quality import document_diagnostics
from pipeline_settings import PipelineSettings, policy_digest
from pipeline_trace import current_trace, event, trace_request
from query_cache import RetrievalScope, access_scope, cached_compute, retrieval_cached
from query_context import QueryContext, resolve_combined, use_context
from repositories import ChatRepository
from token_usage import track_query_usage
from verification import quotes_match, valid_audit


@pytest.fixture(autouse=True)
def pipeline_defaults(monkeypatch):
    for flag in (
        "RAG_QUERY_CONTEXT_V2",
        "RAG_PRUNE_INEFFECTIVE_DECISIONS",
        "RAG_ADAPTIVE_RETRIEVAL",
        "RAG_EMBEDDING_CACHE",
        "RAG_RETRIEVAL_CACHE",
        "RAG_ANSWER_CACHE",
        "RAG_BATCH_NEIGHBORS",
        "RAG_OPTIONAL_FOLLOWUP",
        "RAG_EXTRACTION_DIAGNOSTICS",
        "RAG_HTTP_POOLING",
    ):
        monkeypatch.setenv(flag, "false")
    for flag in ("RAG_PLANNING_POLICY", "RAG_ROUTE_POLICY", "RAG_EVIDENCE_POLICY", "RAG_VERIFICATION_POLICY", "RAG_CONTEXT_RESOLVER"):
        monkeypatch.setenv(flag, "legacy")
    monkeypatch.setenv("RAG_RETRIEVAL_CONCURRENCY", "1")
    monkeypatch.setenv("JEV_SHADOW_SAMPLE_RATE", "1")


def source(**kwargs):
    return {
        "id": "c1",
        "document_id": "d1",
        "source": "synthetic.txt",
        "chunk_index": 0,
        "text": "Submit Form A with proof of ownership. Obtain approval before transport.",
        "score": 0.9,
        "base_score": 0.9,
        "evidence_role": "matched",
        "metadata": {},
        **kwargs,
    }


class MemoryCache:
    def __init__(self):
        self.values = {}
        self.locks = {}
        self.guard = threading.Lock()

    def get(self, key):
        return json.loads(self.values[key]) if key in self.values else None

    def acquire(self, key, owner):
        with self.guard:
            if key in self.locks:
                return False
            self.locks[key] = owner
            return True

    def publish(self, key, owner, value, ttl):
        with self.guard:
            if self.locks.get(key) != owner:
                return False
            self.values[key] = json.dumps(value)
            del self.locks[key]
            return True

    def release(self, key, owner):
        with self.guard:
            if self.locks.get(key) == owner:
                del self.locks[key]

    def close(self):
        pass


@pytest.fixture
def cache(monkeypatch):
    value = MemoryCache()
    monkeypatch.setattr(query_cache, "backend", lambda: value)
    monkeypatch.setenv("RAG_CACHE_HMAC_KEY", "synthetic-only-test-key-32-bytes-minimum")
    return value


def test_context_is_immutable_and_preserves_latest_corrections():
    context = QueryContext.create(
        "Does it apply?",
        resolved="Does the permit apply in Goa as of 2020?",
        history=[
            {"role": "user", "content": "Use Delhi in 2026."},
            {"role": "user", "content": "Correction: Goa as of 2020. Answer in Hindi."},
            {"role": "assistant", "content": "First: obtain approval."},
        ],
        status="resolved",
    )
    assert "Goa as of 2020" in context.policy_question()
    assert "later corrections prevail" in context.policy_question()
    assert "Answer in Hindi" in context.policy_question()
    with pytest.raises(FrozenInstanceError):
        context.resolved_question = "Other"
    copied = context.messages()
    copied[0]["content"] = "Changed"
    assert context.history[0].content == "Use Delhi in 2026."


def test_no_history_combined_resolver_makes_no_model_call(monkeypatch):
    monkeypatch.setattr(retrieval, "generate_structured_with_gemini", lambda *a, **kw: pytest.fail("No call needed"))
    assert resolve_combined([], "Fee?").resolved_question == "Fee?"


def test_combined_keeps_user_constraints_last_reply_and_quote(monkeypatch):
    history = [
        {"role": "user", "content": "Only Goa; answer in Hindi."},
        {"role": "assistant", "content": "First: obtain approval."},
        {"role": "assistant", "content": "An unrelated garden."},
        {"role": "assistant", "content": "Last reply."},
    ]
    monkeypatch.setattr(
        retrieval,
        "generate_structured_with_gemini",
        lambda *a, **kw: {
            "query": "Quote the approval condition for Goa.",
            "keep_assistant_indices": [1],
            "unresolved": [],
            "constraints": [{"kind": "jurisdiction", "value": "Goa", "message_index": 0, "start": 5, "end": 8}],
        },
    )
    ctx = resolve_combined(history, "Quote your first condition exactly.")
    assert [m.content for m in ctx.history] == [history[0]["content"], history[1]["content"], history[3]["content"]]
    assert ctx.constraints[0].value == "Goa"
    assert ctx.jurisdiction == "Goa"


@pytest.mark.parametrize("defect", ["offset", "assistant_constraint", "index", "ambiguous"])
def test_bad_or_uncertain_resolution_retains_history(monkeypatch, defect):
    history = [{"role": "user", "content": "Goa"}, {"role": "assistant", "content": "Prior reply"}]
    result = {"query": "Resolved", "keep_assistant_indices": [], "unresolved": [], "constraints": []}
    if defect == "offset":
        result["constraints"] = [{"kind": "jurisdiction", "value": "Delhi", "message_index": 0, "start": 0, "end": 3}]
    if defect == "assistant_constraint":
        result["constraints"] = [{"kind": "jurisdiction", "value": "Prior", "message_index": 1, "start": 0, "end": 5}]
    if defect == "index":
        result["keep_assistant_indices"] = [999]
    if defect == "ambiguous":
        result["unresolved"] = ["Which permit?"]
    monkeypatch.setattr(retrieval, "generate_structured_with_gemini", lambda *a, **kw: result)
    ctx = resolve_combined(history, "That one?")
    assert len(ctx.history) == len(history)
    assert ctx.unresolved


def test_recent_history_queries_newest_and_omits_sources():
    calls = []

    class Builder:
        def __getattr__(self, name):
            def call(*args, **kwargs):
                calls.append((name, args, kwargs))
                return SimpleNamespace(data=[{"id": "3"}, {"id": "2"}, {"id": "1"}]) if name == "execute" else self

            return call

    client = SimpleNamespace(table=lambda _: Builder())
    repo = ChatRepository(client)
    repo.assert_session_owner = lambda *a: calls.append(("owner", (), {}))
    assert repo.get_recent_messages_for_answer("s", "u", 3) == [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    assert calls[0][0] == "owner"
    assert "sources" not in next(a[0] for n, a, kw in calls if n == "select")
    assert "metadata" in next(a[0] for n, a, kw in calls if n == "select")
    assert [kw for n, a, kw in calls if n == "order"] == [{"desc": True}, {"desc": True}]


def test_pruning_skips_locked_route_and_disabled_plan(monkeypatch):
    monkeypatch.setenv("RAG_PRUNE_INEFFECTIVE_DECISIONS", "true")
    monkeypatch.setenv("JEV_ROUTING_MODE", "active")
    monkeypatch.setenv("JEV_PLANNING_MODE", "active")
    monkeypatch.setenv("RAG_EVIDENCE_PLANNING", "false")
    monkeypatch.setattr(jev_shadow, "evaluate", lambda *a, **kw: pytest.fail("Ineffective request"))
    assert jev_shadow.answer_policy("Current rule?", "[1]", "", baseline_route="answer_complex_high", baseline_planning=False) == {}


@pytest.mark.parametrize(
    "baseline,allowed,removable,expected",
    [(False, True, False, True), (True, True, False, False), (True, True, True, True), (False, False, True, False)],
)
def test_planning_prune_truth_table(baseline, allowed, removable, expected):
    assert decision_policy.effective_questions(
        route="answer_complex_high", baseline_plan=baseline, allowed=allowed, removable_plan=removable
    ) == (False, expected)


def test_shared_decision_keeps_effective_plan_only(monkeypatch):
    monkeypatch.setenv("RAG_PRUNE_INEFFECTIVE_DECISIONS", "true")
    monkeypatch.setenv("JEV_ROUTING_MODE", "active")
    monkeypatch.setenv("JEV_PLANNING_MODE", "active")
    monkeypatch.setenv("RAG_EVIDENCE_PLANNING", "true")
    seen = []
    monkeypatch.setattr(jev_shadow, "evaluate", lambda state, questions, **kw: seen.append(questions) or {"planning": 0.99})
    result = jev_shadow.answer_policy("Current rule?", "[1]", "", baseline_route="answer_complex_high", baseline_planning=False)
    assert result == {"planning": True}
    assert set(seen[0]) == {"planning"}


def test_shadow_sampling_deterministic_and_does_not_disable_active(monkeypatch):
    monkeypatch.setenv("JEV_SHADOW_SAMPLE_RATE", "0")
    assert not decision_policy.sample_shadow("routing", "request")
    import jev_policy

    monkeypatch.setenv("JEV_ROUTING_MODE", "active")
    assert jev_policy.enabled("routing") == "active"
    monkeypatch.setenv("JEV_ROUTING_MODE", "shadow")
    assert jev_policy.enabled("routing") == "off"


def test_planning_shortcut_excludes_exception_and_unknown_scope():
    assert coherent_synthesis("Give an overview of permit requirements.", [source()])
    assert not coherent_synthesis("Give an overview of permit requirements.", [source(text="Except bamboo needs no fee.")])
    assert not coherent_synthesis("Give a comprehensive overview of all permit requirements.", [source()])
    assert not coherent_synthesis("Which rule applies currently?", [source()])
    assert not coherent_synthesis("Give an overview.", [source(document_id=None)])


def test_incidental_amendment_requires_reviewed_principal_instrument(monkeypatch):
    monkeypatch.setenv("RAG_ROUTE_POLICY", "v2")
    ctx = source(
        metadata={
            "temporal_metadata_version": 1,
            "amendment_references": ["Document mentions amendment"],
            "legal_profile": {"reviewed": True, "instrument_type": "rules", "effective_date": "2024-01-01"},
        }
    )
    assert not material_amendment(ctx)
    assert material_amendment({**ctx, "text": "Subject to the amended exception."})
    assert material_amendment(source(metadata={"amendment_references": ["Amendment"]}))


def test_lab_cannot_escape_to_global_legal_corpus(monkeypatch):
    monkeypatch.setenv("RAG_LEGAL_HIERARCHY", "true")
    import legal_retrieval

    monkeypatch.setattr(legal_retrieval, "retrieve_legal", lambda *a: pytest.fail("Wrong corpus"))
    repo = SimpleNamespace(revision_id="lab-revision")
    monkeypatch.setattr(retrieval, "_retrieve", lambda q, k, r, o: [r.revision_id])
    assert retrieval.retrieve("Question", repository=repo) == ["lab-revision"]


def test_diagnostics_preserve_unknown_sufficiency():
    result = diagnostics("What fee applies currently?", [source(text="Fee 100 except bamboo is exempt.")])
    assert result["qualification_count"] == 1
    assert result["applicability_required"] and result["sufficiency"] == "unverified"


def test_extraction_diagnostics_do_not_reject_hindi_or_short_text():
    good = document_diagnostics([{"page": 1, "text": "वन विभाग अनुमति आवश्यक है।"}, {"page": 2, "text": "Fee"}])
    assert not any(p["review_required"] for p in good["pages"])
    bad = document_diagnostics([{"page": 1, "text": "\ufffd" * 30 + " unreadable"}])
    assert bad["pages"][0]["flags"] == ["replacement_characters"]


def test_v2_direct_answer_is_semantically_audited(monkeypatch):
    monkeypatch.setenv("RAG_VERIFICATION_POLICY", "v2")
    calls = []
    monkeypatch.setattr(prompts, "generate_with_gemini", lambda *a, **kw: "Approval is required [1].")

    def verify(*a, **kw):
        calls.append(kw["operation"])
        return {"supported": True, "unchanged": True, "answer": "", "issues": [], "issue_codes": []}

    monkeypatch.setattr(prompts, "generate_structured_with_gemini", verify)
    with trace_request() as trace:
        assert prompts.answer_with_gemini("Who issues the permit?", [source()]) == "Approval is required [1]."
        assert trace.final_audit == "verify"
    assert calls == ["verify"]


def test_resolved_scope_reaches_planner_draft_and_verifier(monkeypatch):
    monkeypatch.setenv("RAG_QUERY_CONTEXT_V2", "true")
    monkeypatch.setenv("RAG_VERIFICATION_POLICY", "v2")
    monkeypatch.setenv("RAG_RISK_BASED_VERIFICATION", "true")
    seen = []

    def generate(prompt, **kw):
        seen.append((kw["operation"], prompt))
        return "Approval is required [1]."

    def structured(prompt, schema, **kw):
        seen.append((kw["operation"], prompt))
        if kw["operation"] == "plan":
            return {"central_answer": "Approval is required", "conflicts": []}
        return {"supported": True, "unchanged": True, "answer": "", "issues": []}

    monkeypatch.setattr(prompts, "generate_with_gemini", generate)
    monkeypatch.setattr(prompts, "generate_structured_with_gemini", structured)
    ctx = QueryContext.create(
        "Explain those provisions.",
        resolved="Give an overview of the Goa rules as of 2020.",
        history=[{"role": "user", "content": "Goa as of 2020, exclude bamboo."}],
    )
    with use_context(ctx):
        prompts.answer_with_gemini(ctx.original_question, [source()])
    assert any(op == "plan" for op, _ in seen)
    assert all("Goa" in text and "2020" in text and "exclude bamboo" in text for _, text in seen)


def test_audit_schema_rejects_invalid_codes_and_quotes():
    assert valid_audit({"supported": True, "issues": [], "issue_codes": []})
    assert not valid_audit({"supported": "yes", "issues": []})
    assert not valid_audit({"supported": True, "issues": [], "issue_codes": ["made_up"]})
    assert quotes_match("The rule says “approval required” [1].", "The rule: approval required.")
    assert not quotes_match("“No approval needed” [1].", "Approval is required.")


def test_cache_hit_is_deep_copy_and_no_new_upstream_work(cache):
    calls = []

    def compute():
        calls.append(1)
        return [source()]

    options = dict(enabled=True, validate=query_cache.valid_contexts)
    first = cached_compute("retrieval", ["key"], compute, **options)
    first[0]["text"] = "mutated"
    second = cached_compute("retrieval", ["key"], compute, **options)
    assert second[0]["text"] != "mutated" and calls == [1]


def test_cache_does_not_retry_failed_paid_compute(cache):
    calls = []

    def fail():
        calls.append(1)
        raise ValueError("provider failure")

    with pytest.raises(ValueError):
        cached_compute("embedding", [], fail, enabled=True, validate=lambda _: True)
    assert len(calls) == 1 and not cache.locks


def test_stale_generation_not_published_or_served(cache, monkeypatch):
    monkeypatch.setenv("RAG_RETRIEVAL_CACHE", "true")
    generation = [1]
    repo = SimpleNamespace(cache_versions=lambda *a: {"corpus": generation[0], "registry": 0})
    calls = []

    def compute():
        calls.append(1)
        generation[0] += 1
        return [source()]

    with access_scope("user"):
        retrieval_cached(repo, "Fee?", None, {}, compute)
    assert not cache.values and len(calls) == 1


def test_retrieval_cache_keys_isolate_users_revisions_and_policies(cache, monkeypatch):
    monkeypatch.setenv("RAG_RETRIEVAL_CACHE", "true")
    repo = SimpleNamespace(cache_versions=lambda *a: {"corpus": 1, "registry": 0})
    calls = []

    def compute():
        calls.append(1)
        return [source()]

    for user in ["u1", "u1", "u2"]:
        with access_scope(user):
            retrieval_cached(repo, "Fee?", None, {}, compute)
    assert len(calls) == 2
    repo.revision_id = "other"
    with access_scope("u1"):
        retrieval_cached(repo, "Fee?", None, {}, compute)
        monkeypatch.setenv("GEMINI_DIRECT_MODEL", "different-model")
        retrieval_cached(repo, "Fee?", None, {}, compute)
    assert len(calls) == 4


def test_missing_version_disables_cache_without_blocking_answer(cache, monkeypatch):
    monkeypatch.setenv("RAG_RETRIEVAL_CACHE", "true")
    with access_scope("u"):
        assert retrieval_cached(SimpleNamespace(), "Q", None, {}, lambda: [source()])
    assert not cache.values


def test_unconfigured_or_failed_cache_bypasses_once(monkeypatch):
    monkeypatch.setattr(query_cache, "backend", lambda: None)
    calls = []
    assert cached_compute("e", [], lambda: calls.append(1) or 3, enabled=True, validate=lambda _: True) == 3
    assert calls == [1]

    class Broken(MemoryCache):
        def get(self, key):
            raise redis.ConnectionError("offline")

    monkeypatch.setattr(query_cache, "backend", Broken)
    assert cached_compute("e", [], lambda: calls.append(1) or 4, enabled=True, validate=lambda _: True) == 4
    assert len(calls) == 2


def test_normal_coalescing_has_one_provider_call(cache, monkeypatch):
    monkeypatch.setenv("RAG_CACHE_WAIT_MS", "1000")
    calls = []

    def compute():
        calls.append(1)
        time.sleep(0.06)
        return [source()]

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(cached_compute, "r", ["same"], compute, enabled=True, validate=query_cache.valid_contexts) for _ in range(4)
        ]
        assert all(f.result()[0]["id"] == "c1" for f in futures)
    assert calls == [1]


def test_follower_recovers_after_leader_lock_timeout(cache, monkeypatch):
    monkeypatch.setenv("RAG_CACHE_WAIT_MS", "0")
    key = query_cache.cache_key("e", [])
    cache.acquire(key, "dead-owner")
    assert cached_compute("e", [], lambda: 4, enabled=True, validate=lambda _: True) == 4
    assert not cache.values
    assert cache.locks[key] == "dead-owner"  # A follower cannot release another owner.


def test_cache_payload_validation_rejects_nonfinite_values():
    assert not query_cache.valid_embedding([float("nan")], 1)
    assert not query_cache.valid_embedding([0.0, 0.0], 2)
    assert not query_cache.valid_contexts([source(score=float("inf"))])


def test_ordered_concurrency_preserves_result_order_and_context(monkeypatch):
    monkeypatch.setenv("RAG_RETRIEVAL_CONCURRENCY", "2")
    active = [0, 0]
    lock = threading.Lock()

    def search(i):
        with lock:
            active[0] += 1
            active[1] = max(active)
        time.sleep(0.04 if i == 0 else 0.01)
        event("worker", ordinal=i)
        with lock:
            active[0] -= 1
        return i

    with trace_request() as trace:
        assert pipeline_network.ordered_search([0, 1, 2, 3], search, safe=True) == [0, 1, 2, 3]
        assert len([e for e in trace.events if e["code"] == "worker"]) == 4
    assert active[1] == 2
    assert current_trace() is None


def test_concurrency_joins_started_workers_before_error(monkeypatch):
    monkeypatch.setenv("RAG_RETRIEVAL_CONCURRENCY", "2")
    completed = []

    def search(i):
        if i == 0:
            raise RuntimeError("required branch failed")
        time.sleep(0.02)
        completed.append(i)

    with pytest.raises(RuntimeError):
        pipeline_network.ordered_search([0, 1], search, safe=True)
    assert completed == [1]


def test_worker_accounting_has_request_correlation(monkeypatch, caplog):
    import logging

    from token_usage import record_embedding

    monkeypatch.setenv("RAG_RETRIEVAL_CONCURRENCY", "2")

    @track_query_usage
    def query():
        return pipeline_network.ordered_search([1, 2], lambda _: record_embedding("m", None), safe=True)

    with caplog.at_level(logging.INFO):
        query()
    result = next(r for r in caplog.records if r.msg == "query_generation_usage")
    assert result.embedding_calls == 2
    assert result.total_api_cost_usd is None
    assert result.query_id


def test_pipeline_settings_digest_and_validation(monkeypatch):
    first = policy_digest()
    monkeypatch.setenv("RAG_CACHE_EPOCH", "changed")
    assert policy_digest() != first
    monkeypatch.setenv("RAG_RETRIEVAL_CONCURRENCY", "99")
    with pytest.raises(ValueError):
        PipelineSettings.read()


def test_scope_captures_preview_and_lab_identity(monkeypatch):
    with access_scope("u"), use_context(QueryContext.create("Q", jurisdiction="Goa", as_of="2020-01-01")):
        scope = RetrievalScope.build(SimpleNamespace(revision_id="r"), "dev")
    assert scope.kind == "lab" and scope.revision == "r" and scope.namespace == "dev"
    assert scope.access and "u" != scope.access


def test_answer_cache_requires_real_support_audit_and_same_user(cache, monkeypatch):
    import repositories
    from pipeline_trace import audited
    from retrieval import source_payload

    monkeypatch.setenv("RAG_ANSWER_CACHE", "true")
    monkeypatch.setenv("RAG_LEGAL_HIERARCHY", "false")
    monkeypatch.setattr(repositories, "DocumentRepository", lambda: SimpleNamespace(cache_versions=lambda *a: {"corpus": 1, "registry": 0}))
    calls = []
    response = {
        "answer": "The permit fee is INR 100 [1].",
        "outcome": "answered",
        "sources": source_payload([source()]),
        "cited_sources": source_payload([source()]),
        "confidence": 0.9,
        "abstained": False,
    }

    def compute():
        calls.append(1)
        audited("verify", True)
        return response.copy()

    with trace_request(), access_scope("u1"):
        assert query_cache.answer_cached("What is the permit fee?", None, compute) == response
    with trace_request(), access_scope("u1"):
        assert query_cache.answer_cached("What is the permit fee?", None, compute) == response
    assert calls == [1]
    with trace_request(), access_scope("u2"):
        query_cache.answer_cached("What is the permit fee?", None, compute)
    assert len(calls) == 2
    cache.values.clear()

    def unaudited():
        calls.append(1)
        return response

    with trace_request(), access_scope("u1"):
        query_cache.answer_cached("What is the permit fee?", None, unaudited)
    assert not cache.values

    def web_response():
        # Even a stale document-audit approval must not authorize caching web answers.
        audited("verify", True)
        return {**response, "answer_origin": "web"}

    with trace_request(), access_scope("u1"):
        query_cache.answer_cached("What is the permit fee?", None, web_response)
    assert not cache.values


def test_current_answer_cache_bypasses_and_never_creates_db_client(cache, monkeypatch):
    import repositories

    monkeypatch.setenv("RAG_ANSWER_CACHE", "true")
    monkeypatch.setattr(repositories, "DocumentRepository", lambda: pytest.fail("No current answer cache"))
    with access_scope("u1"):
        assert query_cache.answer_cached("What is the current permit fee?", None, lambda: "fresh") == "fresh"


def test_bad_cache_configuration_cannot_repeat_or_block_compute(monkeypatch):
    monkeypatch.setattr(query_cache, "backend", lambda: (_ for _ in ()).throw(ValueError("bad URL")))
    calls = []
    assert cached_compute("e", [], lambda: calls.append(1) or 7, enabled=True, validate=lambda _: True) == 7
    assert len(calls) == 1


def test_combined_chat_does_not_also_run_jev_resolvers(monkeypatch):
    from contextlib import nullcontext

    import chat_service
    import jev_policy

    monkeypatch.setenv("RAG_QUERY_CONTEXT_V2", "true")
    monkeypatch.setenv("RAG_CONTEXT_RESOLVER", "combined")
    monkeypatch.setenv("JEV_HISTORY_MODE", "active")
    monkeypatch.setenv("JEV_REWRITE_MODE", "active")
    history = [{"role": "user", "content": "Only Goa."}]
    monkeypatch.setattr(chat_service, "resolve_combined", lambda m, q: QueryContext.create(q, resolved="Goa permit fee?", history=m))
    monkeypatch.setattr(jev_policy, "standalone_question", lambda *a: pytest.fail("Extra gate"))
    monkeypatch.setattr(jev_shadow, "history_selection", lambda *a: pytest.fail("Extra history call"))
    monkeypatch.setattr(chat_service, "retrieve", lambda *a: [])
    monkeypatch.setattr(chat_service, "answer_with_gemini", lambda *a: prompts.INSUFFICIENT_EVIDENCE_ANSWER)
    repo = SimpleNamespace(
        begin_turn=lambda *a: {"state": "claimed"},
        turn_lease=lambda *a: nullcontext(SimpleNamespace(check=lambda: None)),
        get_recent_messages_for_answer=lambda *a: history,
        complete_turn=lambda *a: a[-1],
    )
    result = chat_service.chat_ask("s", "Fee?", "u", repository=repo)
    assert result["search_query"] == "Goa permit fee?"


def test_v2_keeps_active_jev_rejection_final(monkeypatch):
    import jev_policy

    monkeypatch.setenv("RAG_VERIFICATION_POLICY", "v2")
    monkeypatch.setenv("RAG_LITE_EXTRACTION", "true")
    monkeypatch.setenv("JEV_VERIFICATION_MODE", "active")
    monkeypatch.setattr(prompts, "generate_with_gemini", lambda *a, **kw: "Submit Form A [1].")
    monkeypatch.setattr(jev_policy, "draft_verification_decision", lambda *a, **kw: "rejected")
    monkeypatch.setattr(prompts, "generate_structured_with_gemini", lambda *a, **kw: pytest.fail("No hidden approval retry"))
    assert prompts.answer_with_gemini("List the application steps.", [source()]) == prompts.UNSUPPORTED_ANSWER


def test_all_active_combination_has_one_resolver_and_consistent_policy(monkeypatch):
    import jev_policy

    monkeypatch.setenv("RAG_QUERY_CONTEXT_V2", "true")
    monkeypatch.setenv("RAG_VERIFICATION_POLICY", "v2")
    monkeypatch.setenv("RAG_PRUNE_INEFFECTIVE_DECISIONS", "true")
    monkeypatch.setenv("RAG_ROUTE_POLICY", "v2")
    monkeypatch.setenv("RAG_PLANNING_POLICY", "v2")
    monkeypatch.setenv("RAG_EVIDENCE_POLICY", "v2")
    monkeypatch.setenv("RAG_LITE_EXTRACTION", "true")
    monkeypatch.setenv("RAG_RISK_BASED_VERIFICATION", "true")
    for feature in ("ROUTING", "PLANNING", "RERANK", "REWRITE", "HISTORY", "VERIFICATION"):
        monkeypatch.setenv("JEV_" + feature + "_MODE", "active")

    def evaluate(state, questions, **kw):
        return {name: 0 if name in ("synthesis", "legal_risk", "planning") else 1 for name in questions}

    monkeypatch.setattr(jev_shadow, "evaluate", evaluate)
    monkeypatch.setattr(jev_policy, "evaluate", evaluate)
    monkeypatch.setattr(prompts, "generate_with_gemini", lambda *a, **kw: "Submit Form A and obtain approval [1].")
    monkeypatch.setattr(
        prompts,
        "generate_structured_with_gemini",
        lambda *a, **kw: {"supported": True, "unchanged": True, "answer": "", "issues": [], "issue_codes": []},
    )
    with trace_request() as trace, use_context(QueryContext.create("List the application steps.")):
        assert prompts.answer_with_gemini("List the application steps.", [source()]).endswith("[1].")
        assert trace.final_audit == "jev"


def test_evidence_promotes_relevant_qualification_without_losing_anchor():
    from evidence_policy import protect_qualifications

    candidates = [
        source(id="anchor", text="The timber permit fee is 100."),
        source(id="generic", text="A garden surrounds the office.", score=0.85),
        source(id="condition", text="Timber permits require approval except emergency transport.", score=0.8),
    ]
    ordered = protect_qualifications("What is the timber permit fee?", candidates)
    assert [c["id"] for c in ordered] == ["anchor", "condition", "generic"]
    assert len(ordered) == len(candidates)


def test_known_ancestry_does_not_invent_missing_parent():
    from chunking import known_section_ancestry

    assert known_section_ancestry("1.2 Fees", ["1 Requirements", "1.2 Fees"]) == ["1 Requirements", "1.2 Fees"]
    assert known_section_ancestry("1.2 Fees", []) == ["1.2 Fees"]


def test_unknown_numeric_scope_from_resolver_falls_back(monkeypatch):
    monkeypatch.setattr(
        retrieval,
        "generate_structured_with_gemini",
        lambda *a, **kw: {"query": "Goa permit as of 2039", "keep_assistant_indices": [], "unresolved": [], "constraints": []},
    )
    ctx = resolve_combined([{"role": "user", "content": "Use 2020."}], "Which fee?")
    assert ctx.status == "fallback" and ctx.resolved_question == "Which fee?"


def test_evaluation_budget_and_replay_are_explicit(tmp_path):
    from scripts.evaluate_pipeline import Budget, BudgetExceeded, main

    budget = Budget(1, 100000, 10)
    payload = {"generationConfig": {"maxOutputTokens": 100}}
    budget.before(payload, "gemini-3.5-flash-lite")
    with pytest.raises(BudgetExceeded):
        budget.before(payload, "gemini-3.5-flash-lite")
    report = tmp_path / "evaluation.json"
    assert main(["--output", str(report), "--modes", "baseline", "combined"]) == 0
    result = json.loads(report.read_text())
    assert "synthetic" in result["scope"]
    assert result["summary"]["combined"]["expert_quality"] == "not_measured"
    assert not any(row.get("error") for row in result["results"])


def test_extraction_pilot_retains_fees_units_and_qualifications():
    from scripts.evaluate_extraction import run_pilot

    report = run_pilot()
    assert all(all(r["checks"].values()) for r in report["results"])
    assert next(r for r in report["results"] if r["method"] == "pdf-table-aware")["table_units"] > 0


def test_merged_title_row_keeps_actual_column_labels(monkeypatch, tmp_path):
    from docx import Document

    from chunking import chunk_document
    from documents import read_docx

    document = Document()
    table = document.add_table(rows=2, cols=2)
    table.rows[0].cells[0].merge(table.rows[0].cells[1]).text = "Permit fee schedule (INR)"
    table.rows[1].cells[0].text = "Species"
    table.rows[1].cells[1].text = "Fee (INR)"
    row = table.add_row().cells
    row[0].text, row[1].text = "Timber", "1,000"
    path = tmp_path / "merged.docx"
    document.save(path)
    monkeypatch.setenv("RAG_EXTRACTION_DIAGNOSTICS", "true")
    pages = read_docx(path)
    block = pages[0]["blocks"][0]
    assert block["headers"] == ["Species", "Fee (INR)"]
    assert block["caption"] == "Permit fee schedule (INR)"
    chunks = chunk_document({"source": "merged.docx", "kind": "docx", "title": "Schedule", "pages": pages, "metadata": {}})
    assert "Species: Timber; Fee (INR): 1,000" in chunks[0]["content"]
    assert "Caption: Permit fee schedule (INR)" in chunks[0]["content"]


def test_v2_current_applicability_audit_is_strong_even_with_legacy_risk_flag_off(monkeypatch):
    monkeypatch.setenv("RAG_VERIFICATION_POLICY", "v2")
    monkeypatch.setenv("RAG_RISK_BASED_VERIFICATION", "false")
    operations = []
    monkeypatch.setattr(prompts, "generate_with_gemini", lambda *a, **kw: "The supplied permit fee is 100 [1].")

    def structured(*a, **kw):
        operations.append(kw["operation"])
        return {"supported": True, "unchanged": True, "answer": "", "issues": [], "issue_codes": []}

    monkeypatch.setattr(prompts, "generate_structured_with_gemini", structured)
    prompts.answer_with_gemini("What permit fee applies currently?", [source()])
    assert operations == ["verify_complex"]


def test_v2_lexical_terms_preserve_hindi_marks_and_negation(monkeypatch):
    monkeypatch.setenv("RAG_EVIDENCE_POLICY", "v2")
    terms = retrieval.meaningful_terms("वन परिवहन अनुमति नहीं है; Rule 3.4 applies.")
    assert {"वन", "परिवहन", "अनुमति", "नहीं", "3.4"} <= terms
    assert "है" not in terms
    monkeypatch.setenv("RAG_EVIDENCE_POLICY", "legacy")
    assert "परिवहन" not in retrieval.meaningful_terms("वन परिवहन अनुमति नहीं है")


def test_live_budget_refuses_unpriced_embeddings_before_spending(monkeypatch):
    from scripts.evaluate_pipeline import Budget, BudgetExceeded

    monkeypatch.delenv("COST_EMBEDDING_USD_PER_MILLION_TOKENS", raising=False)
    monkeypatch.delenv("COST_EMBEDDING_RATE_MODEL", raising=False)
    budget = Budget(5, 100000, 1)
    with pytest.raises(BudgetExceeded):
        budget.before({"content": {"parts": [{"text": "synthetic"}]}}, "gemini-embedding-2")
    assert budget.requests == 0
    monkeypatch.setenv("COST_EMBEDDING_RATE_MODEL", "gemini-embedding-2")
    monkeypatch.setenv("COST_EMBEDDING_USD_PER_MILLION_TOKENS", "0.2")
    budget.before({"content": {"parts": [{"text": "synthetic"}]}}, "gemini-embedding-2")
    budget.after("gemini-embedding-2", "embedContent", {"usageMetadata": {"promptTokenCount": 10}})
    assert budget.tokens == 10 and budget.usd > 0 and not budget.incomplete
