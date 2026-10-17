"""Offline acceptance tests for Jev ownership, independent audits and cost completeness."""

import json
import logging
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
import requests

import jev_shadow
import prompts
from errors import AppError, ErrorCode
from generation_cost import embedding_cost, generation_cost, normalized_usage
from pipeline_settings import policy_digest
from pipeline_trace import trace_request
from query_context import QueryContext, use_context
from query_cost_policy import confident_category, safeguards, select_route, verification_reason
from services import jev
from services.gemini import GeminiClient
from token_usage import record_decision, record_embedding, record_generation, track_query_usage


def source(text="The fee is INR 100. Bamboo is exempt but requires approval.", **changes):
    return {"document_id": "doc", "source": "rules.txt", "section_heading": "Fees", "text": text,
            "score": .95, "metadata": {"temporal_metadata_version": 1, "amendment_references": ["Rule 2"]}, **changes}


@pytest.fixture
def candidate(monkeypatch):
    monkeypatch.setenv("RAG_ROUTING_POLICY", "jev-led-v1")
    monkeypatch.setenv("RAG_AUDIT_POLICY", "independent-v1")
    monkeypatch.setenv("RAG_EVIDENCE_COMPACTION", "exact-v1")
    monkeypatch.setenv("JEV_ROUTING_MODE", "active")
    monkeypatch.setenv("RAG_EVIDENCE_PLANNING", "false")
    monkeypatch.setattr(prompts, "retrieval_is_confident", lambda rows: True)


@pytest.mark.parametrize("scores,expected", [
    ({"bounded": .8, "complex": .6, "reconciliation": .1}, "bounded"),
    ({"bounded": .9, "complex": .9, "reconciliation": .1}, None),
    ({"bounded": .7, "complex": .1, "reconciliation": .1}, None),
    ({"bounded": True, "complex": .1, "reconciliation": .1}, None),
    ({"bounded": float("nan"), "complex": .1, "reconciliation": .1}, None),
    ({"bounded": 1, "complex": 0}, None),
])
def test_category_confidence_and_margin(scores, expected):
    assert confident_category(scores) == expected


@pytest.mark.parametrize("question", ["What is the fee?", "What was the fee in 2020?", "शुल्क कितना है?", "List the steps after amendment."])
def test_jev_can_downgrade_legacy_high_without_losing_amendment(question, candidate):
    rows = [source()]
    original = deepcopy(rows)
    proposed = {"scores": {"bounded": .95, "complex": .1, "reconciliation": .05}, "route": "answer_direct"}
    assert jev_shadow.apply_route(proposed, "answer_complex_high", question, rows, evidence_plan=None, history=True) == "answer_direct"
    assert not safeguards(question, rows)
    assert rows == original


@pytest.mark.parametrize("scores,expected", [
    ({"bounded": .1, "complex": .9, "reconciliation": .1}, "answer_complex"),
    ({"bounded": .1, "complex": .2, "reconciliation": .9}, "answer_complex_high"),
])
def test_jev_promotion(candidate, scores, expected):
    assert select_route("Explain", [source()], {"scores": scores}, active=True).operation == expected


@pytest.mark.parametrize("question", ["Which rule applies to this project?", "Does this apply to my case?",
                                      "How do the amended rules affect me?", "Interpret the court judgment",
                                      "मेरे मामले में कौन सा नियम लागू है?"])
def test_actual_reconciliation_overrides_cheap_proposal(candidate, question):
    result = select_route(question, [source()], {"scores": {"bounded": .99, "complex": 0, "reconciliation": 0}}, active=True)
    assert result.operation == "answer_complex_high" and result.owner == "safeguard"


def test_confirmed_conflicts_and_judicial_interpretation_are_protected(candidate):
    rows = [source(metadata={"legal_review": {"confirmed_conflict": True}})]
    assert safeguards("What is the fee?", rows) == ("confirmed_evidence_conflict",)
    rows = [source(metadata={"legal_profile": {"instrument_type": "judicial", "reviewed": True}})]
    assert safeguards("Interpret the holding", rows) == ("judicial_interpretation",)
    assert not safeguards("What was the judgment date?", rows)


def test_uncertainty_and_missing_evidence_do_not_force_high(candidate):
    assert select_route("What is the fee?", [source()], {}, active=True).operation == "answer_direct"
    assert select_route("What is the fee?", [], {}, active=True).operation == "answer_complex"
    context = QueryContext.create("That one?", unresolved=("referent",))
    with use_context(context):
        assert select_route("That one?", [source()], {}, active=True).operation == "answer_complex"
    assert select_route("What is the fee?", [source()], {}, {"unknowns": ["missing evidence"]}, active=True).operation == "answer_complex"


def test_single_combined_call_not_pruned_by_legacy_high(candidate, monkeypatch):
    monkeypatch.setenv("RAG_EVIDENCE_PLANNING", "true")
    monkeypatch.setenv("JEV_PLANNING_MODE", "active")
    monkeypatch.setenv("RAG_PRUNE_INEFFECTIVE_DECISIONS", "true")
    seen = []

    def evaluate(state, checks, **kwargs):
        seen.append((state, checks))
        return {"bounded": .95, "complex": .1, "reconciliation": .05, "planning": .99}

    monkeypatch.setattr(jev_shadow, "evaluate", evaluate)
    proposal = jev_shadow.answer_policy("Question", "Evidence", "History", baseline_route="answer_complex_high", baseline_planning=False)
    assert len(seen) == 1 and set(seen[0][1]) == {"bounded", "complex", "reconciliation", "planning"}
    assert proposal["route"] == "answer_direct"
    # A later plan conflict overrides without calling Jev again.
    assert jev_shadow.apply_route(proposal, "answer_complex_high", "Question", [source()],
                                  evidence_plan={"conflicts": ["Different fees"]}, history=False) == "answer_complex_high"
    assert len(seen) == 1


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_off_shadow_preserve_baseline(candidate, monkeypatch, mode):
    monkeypatch.setenv("JEV_ROUTING_MODE", mode)
    assert jev_shadow.apply_route({"scores": {"bounded": 1, "complex": 0, "reconciliation": 0}},
                                  "answer_complex_high", "Fee?", [source()], evidence_plan=None, history=False) == "answer_complex_high"


@pytest.mark.parametrize("failure", ["timeout", "oversized", "malformed"])
def test_jev_transport_failures_use_routing_fallback(candidate, monkeypatch, failure):
    monkeypatch.setenv("JEV_KEY", "offline-test")
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-test")

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def json(self):
            return {"answers": {"wrong": {"type": "noul", "noul": .99}}}

    def post(*args, **kwargs):
        if failure == "oversized":
            pytest.fail("Oversized state must not be transmitted")
        if failure == "timeout":
            raise requests.Timeout("offline")
        return Response()

    monkeypatch.setattr(jev, "pooled_post", post)
    proposal = jev_shadow.answer_policy("Fee?", "x" * 61000 if failure == "oversized" else "Evidence", "",
                                       baseline_route="answer_complex_high", baseline_planning=False)
    assert proposal["routing_status"] == "jev_unavailable"
    assert jev_shadow.apply_route(proposal, "answer_complex_high", "Fee?", [source()], evidence_plan=None, history=False) == "answer_direct"


def install_models(monkeypatch, draft="The fee is INR 100 [1].", scores=None):
    calls, payloads = [], []
    scores = scores or {"bounded": .1, "complex": .9, "reconciliation": .1}
    monkeypatch.setattr(jev_shadow, "evaluate", lambda *a, **kw: scores)

    def generate(prompt, **kwargs):
        calls.append(kwargs["operation"])
        payloads.append(prompt)
        return draft

    def audit(prompt, schema, **kwargs):
        calls.append(kwargs["operation"])
        payloads.append(prompt)
        return {"supported": True, "unchanged": True, "answer": "", "issues": []}

    monkeypatch.setattr(prompts, "generate_with_gemini", generate)
    monkeypatch.setattr(prompts, "generate_structured_with_gemini", audit)
    return calls, payloads


def test_medium_direct_answer_now_audited_with_lite(candidate, monkeypatch):
    calls, _ = install_models(monkeypatch)
    assert prompts.answer_with_gemini("What is the fee?", [source()]) == "The fee is INR 100 [1]."
    assert calls == ["answer_complex", "verify"]


def test_high_drafting_does_not_automatically_select_strong_verifier(candidate, monkeypatch):
    # Independently enable auditing while retaining the legacy drafting route.
    monkeypatch.setenv("RAG_ROUTING_POLICY", "legacy")
    monkeypatch.setenv("JEV_ROUTING_MODE", "off")
    calls, _ = install_models(monkeypatch)
    prompts.answer_with_gemini("What is the fee?", [source()])
    assert calls == ["answer_complex_high", "verify"]


def test_draft_reconciliation_claim_selects_strong_audit(candidate, monkeypatch):
    calls, _ = install_models(monkeypatch, "This rule overrides the earlier rule [1].",
                              {"bounded": .95, "complex": .1, "reconciliation": .05})
    prompts.answer_with_gemini("What is the fee?", [source()])
    assert calls == ["answer_direct", "verify_complex"]


def test_shared_resolved_context_reaches_all_stages_once(candidate, monkeypatch):
    monkeypatch.setenv("RAG_EVIDENCE_PLANNING", "true")
    seen = []

    def evaluate(state, checks, **kwargs):
        seen.append(state)
        return {"bounded": .1, "complex": .9, "reconciliation": .1}

    calls, payloads = install_models(monkeypatch)
    monkeypatch.setattr(jev_shadow, "evaluate", evaluate)
    plan_prompts = []
    monkeypatch.setattr(prompts, "build_evidence_plan",
                        lambda question, evidence: plan_prompts.append(question) or {"central_answer": "Fee"})
    context = QueryContext.create("And the fee?", resolved="Timber permit fee in Green State in 2020",
                                  history=[{"role": "user", "content": "Keep bamboo exemption and answer in Hindi."}])
    with use_context(context):
        prompts.answer_with_gemini("And the fee?", [source()], context.messages())
    assert len(seen) == 1 and calls == ["answer_complex", "verify"]
    for prompt in [seen[0]["question"], *payloads, *plan_prompts]:
        assert "Timber permit fee in Green State in 2020" in prompt
        assert prompt.count("Keep bamboo exemption and answer in Hindi.") == 1


def test_negative_jev_verdict_is_final(candidate, monkeypatch):
    monkeypatch.setenv("JEV_VERIFICATION_MODE", "active")
    monkeypatch.setattr("jev_policy.draft_verification_decision", lambda *a, **kw: "rejected")
    calls, _ = install_models(monkeypatch)
    assert prompts.answer_with_gemini("What is the fee?", [source()]) == prompts.UNSUPPORTED_ANSWER
    assert calls == ["answer_complex"]


@pytest.mark.parametrize("technical", [False, True])
def test_audit_failure_has_bounded_escalation(candidate, monkeypatch, technical):
    calls, _ = install_models(monkeypatch)

    def audit(*args, **kwargs):
        calls.append(kwargs["operation"])
        if technical:
            raise AppError("offline failure", code=ErrorCode.UPSTREAM_ERROR)
        return {"supported": False, "unchanged": False, "answer": prompts.UNSUPPORTED_ANSWER, "issues": ["unsupported"]}

    monkeypatch.setattr(prompts, "generate_structured_with_gemini", audit)
    assert prompts.answer_with_gemini("What is the fee?", [source()]) == prompts.UNSUPPORTED_ANSWER
    assert calls == (["answer_complex", "verify"] if technical else ["answer_complex", "verify", "verify_complex"])


def test_exact_quote_rejection_and_uncited_exception_available(candidate, monkeypatch):
    calls, payloads = install_models(monkeypatch, 'The fee is "INR 500" [1].')
    assert prompts.answer_with_gemini("Fee?", [source()]) == prompts.UNSUPPORTED_ANSWER
    assert calls == ["answer_complex", "verify", "verify_complex"]
    assert all("Bamboo is exempt but requires approval" in prompt for prompt in payloads)


def test_evidence_compaction_preserves_ids_and_all_material_evidence(candidate):
    text = "Submit Form A with proof of ownership. " * 20
    rows = [source(text), source(text + "Bamboo is exempt but requires approval.")]
    original = deepcopy(rows)
    with trace_request() as trace:
        compact = prompts.format_contexts(rows)
    assert compact.count(text) == 1 and "[1]" in compact and "[2]" in compact
    assert "Bamboo is exempt but requires approval" in compact
    assert rows == original
    metric = next(e for e in trace.events if e["code"] == "evidence_compaction")
    assert metric["after_tokens"] < metric["before_tokens"]
    assert prompts.validate_answer_citations("Submit Form A [1, 2].", 2) != prompts.UNSUPPORTED_ANSWER


def test_compaction_preserves_page_labels_despite_different_retrieval_ranks(candidate):
    text = "Submit Form A with proof of ownership. " * 20
    first, second = source(text, page_start=1, page_end=1), source(text + "Bamboo is exempt.", page_start=2, page_end=2)
    first["metadata"]["retrieval_fusion"] = {"best_rank": 1}
    second["metadata"]["retrieval_fusion"] = {"best_rank": 2}
    compact = prompts.format_contexts([first, second])
    assert compact.count(text) == 1
    assert "rules.txt, page 1" in compact and "rules.txt, page 2" in compact


@pytest.mark.parametrize("difference", ["document", "date", "section", "version", "scope", "number", "negation"])
def test_compaction_does_not_merge_distinct_provisions(candidate, difference):
    row = source("The fee is INR 100. " * 30)
    other = deepcopy(row)
    if difference == "document":
        other["document_id"] = "other"
    elif difference == "date":
        other["metadata"]["effective_date"] = "2020-01-01"
    elif difference == "section":
        other["section_heading"] = "Other"
    elif difference == "version":
        other["metadata"]["version"] = "2"
    elif difference == "scope":
        other["metadata"]["jurisdiction"] = "Other State"
    elif difference == "number":
        other["text"] = other["text"].replace("100", "200")
    else:
        other["text"] = other["text"].replace("is", "is not")
    formatted = prompts.format_contexts([row, other])
    assert "contained verbatim" not in formatted
    assert row["text"] in formatted and other["text"] in formatted
    if difference in {"document", "date", "section", "version", "scope"}:
        assert formatted.count(row["text"]) == 2


@pytest.mark.parametrize("short,long", [("INR 100", "INR 1000"), ("INR 100", "INR 100.50"),
                                       ("Act 20", "Act 2020"), ("शुल्क", "शुल्कों")])
def test_number_and_word_prefixes_never_alias(candidate, short, long):
    from evidence_bundle import literal_span

    assert literal_span(short, long) is None


def test_plan_compaction_keeps_generated_conclusions(candidate):
    from evidence_bundle import compact_plan

    text = "Submit Form A and proof of ownership. " * 20
    plan = {"central_answer": text, "conflicts": [], "unknowns": ["Unclear jurisdiction"],
            "themes": [{"claim": "A generated conclusion with distinct wording", "source_ids": [1]}]}
    result = json.loads(compact_plan(plan, [source(text)]))
    assert result["central_answer"] == "Verbatim evidence: see excerpt [1]."
    assert result["unknowns"] == plan["unknowns"] and result["themes"] == plan["themes"]


def test_cache_identity_includes_policies_thresholds_and_verifier_limits(candidate, monkeypatch):
    hashes = [policy_digest()]
    for key, value in [("RAG_ROUTING_POLICY", "legacy"), ("JEV_ROUTE_THRESHOLD", ".9"),
                       ("GEMINI_VERIFY_COMPLEX_MAX_OUTPUT_TOKENS", "4500"), ("RAG_EVIDENCE_COMPACTION", "legacy")]:
        monkeypatch.setenv(key, value)
        hashes.append(policy_digest())
    assert len(set(hashes)) == len(hashes)
    monkeypatch.setenv("GEMINI_API_KEY", "different-secret")
    assert policy_digest() == hashes[-1]


def test_separate_strong_verifier_profile(candidate, monkeypatch):
    monkeypatch.setenv("GEMINI_COMPLEX_MAX_OUTPUT_TOKENS", "9000")
    monkeypatch.setenv("GEMINI_COMPLEX_THINKING_LEVEL", "high")
    payloads = []

    def post(model, action, payload, **kwargs):
        payloads.append(deepcopy(payload))
        return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "Approved"}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2, "totalTokenCount": 12}}

    client = GeminiClient()
    monkeypatch.setattr(client, "_post", post)
    client.generate("Evidence", operation="verify_complex")
    assert payloads[0]["generationConfig"]["maxOutputTokens"] == 4000
    assert payloads[0]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "medium"


def test_missing_usage_and_future_pricing(monkeypatch):
    monkeypatch.setenv("COST_USD_TO_INR", "90")
    usage = {"promptTokenCount": 100, "candidatesTokenCount": 20, "totalTokenCount": 150}
    assert normalized_usage(usage)["thoughtsTokenCount"] == 30
    assert "thoughtsTokenCount" not in usage
    assert "thoughtsTokenCount" not in normalized_usage({"totalTokenCount": 100})
    assert generation_cost("gemini-3.8-flash", {**usage, "thoughtsTokenCount": 10})["estimated_cost_inr"] is None
    now = generation_cost("gemini-3.8-flash", usage, as_of=date(2026, 10, 17))
    later = generation_cost("gemini-3.8-flash", usage, as_of=date(2027, 1, 1))
    assert later["estimated_cost_inr"] == 2 * now["estimated_cost_inr"]
    embedding = embedding_cost("gemini-embedding-2", {"promptTokenCount": 1000}, as_of=date(2026, 10, 17))
    assert embedding["estimated_cost_inr"] == pytest.approx(.018)
    assert embedding_cost("unknown", {"promptTokenCount": 1000})["estimated_cost_inr"] is None
    assert embedding_cost("gemini-embedding-2", {})["estimated_cost_inr"] is None


@pytest.mark.parametrize("complete", [True, False])
def test_all_api_targets_raw_usage_and_reconciliation(candidate, caplog, complete):
    raw = {"promptTokenCount": 1000, "candidatesTokenCount": 100, "totalTokenCount": 1120}

    @track_query_usage
    def run():
        record_generation("answer_direct", "gemini-3.5-flash-lite", raw)
        record_embedding("gemini-embedding-2", {"promptTokenCount": 100} if complete else {})
        record_decision("jev_answer_policy", "jev-1", {"input_tokens": 1000}, elapsed_ms=1, succeeded=True)

    with caplog.at_level(logging.INFO):
        run()
    detailed = next(r for r in caplog.records if r.message == "query_generation_usage")
    summary = next(r for r in caplog.records if r.message == "query_answer_cost")
    assert detailed.thinking_tokens == 20 and detailed.cached_tokens == 0
    assert detailed.stage_costs[0]["raw_usage"] == raw
    assert summary.api_cost_target_inr == 1.2
    assert summary.api_cost_target_status == ("within_target" if complete else "unknown")
    assert summary.total_api_cost_inr == detailed.total_api_cost_inr
    known = sum(c["estimated_cost_inr"] or 0 for c in [*detailed.stage_costs, *detailed.embedding_stages, *detailed.decision_stages])
    assert summary.known_api_cost_inr == pytest.approx(known)


def test_verifier_risk_is_independent_of_operation():
    assert verification_reason("Fee?", [source()], "The fee is INR 100 [1].") == "bounded_support_audit"
    assert verification_reason("Fee?", [source()], "This rule takes precedence over the old one [1].") == "draft_reconciliation_claim"


def test_historical_log_receipts_reconcile_without_counting_summaries_twice():
    from scripts.evaluate_query_cost_policy import historical_report

    fixture = json.loads((Path(__file__).parent / "fixtures/query_cost_logs.json").read_text())
    queries = historical_report(fixture["events"])
    completed = [q for q in queries if q["outcome"] == "answered"]
    assert [q["known_api_cost_inr"] for q in completed] == pytest.approx([
        .4927869, .96157296, 2.25857304, 3.390633, 4.53627342,
    ])
    assert all(q["reconciled"] and q["embedding_cost_inr"] is None and q["total_api_cost_inr"] is None for q in completed)
    interrupted = [q for q in queries if not q["completion_recorded"]]
    assert len(interrupted) == 1 and interrupted[0]["known_api_cost_inr"] == pytest.approx(.5865894)
    assert interrupted[0]["reconciled"] is None
    assert len([q for q in queries if q["outcome"] == "failed"]) == 3


@pytest.mark.parametrize("embedding", [True, False])
def test_http_retries_count_unknown_attempts_separately(candidate, monkeypatch, caplog, embedding):
    monkeypatch.setenv("GEMINI_API_KEY", "offline")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "768")
    monkeypatch.setenv("GEMINI_GENERATION_MAX_RETRIES", "1")
    monkeypatch.setenv("GEMINI_EMBEDDING_MAX_RETRIES", "1")
    client = GeminiClient()
    monkeypatch.setattr(client, "_retry_delay", lambda *args: 0)
    monkeypatch.setattr(client, "_pace_requests", lambda *args: None)
    count = []

    class Response:
        text = "offline"

        def __init__(self, status):
            self.status_code = status

        def json(self):
            if embedding:
                return {"embedding": {"values": [1.] * 768}, "usageMetadata": {"promptTokenCount": 3}}
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "Answer"}]}}],
                    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5}}

    def post(*args, **kwargs):
        count.append(1)
        return Response(500 if len(count) == 1 else 200)

    monkeypatch.setattr("services.gemini.pooled_post", post)

    @track_query_usage
    def query():
        return client.embed("Fee?", "RETRIEVAL_QUERY") if embedding else client.generate("Question")

    with caplog.at_level(logging.INFO):
        query()
    summary = next(r for r in caplog.records if r.message == "query_answer_cost")
    detail = next(r for r in caplog.records if r.message == "query_generation_usage")
    assert len(count) == detail.provider_attempt_count == 2
    assert summary.total_api_cost_inr is None and summary.api_cost_target_status == "unknown"
    if embedding:
        assert detail.embedding_calls == 2 and detail.embedding_stages[0]["estimated_cost_inr"] is None
    else:
        assert detail.generation_calls == 1 and detail.unmetered_generation_attempts == 1
        assert detail.unmetered_stages[0]["attempt"] == 1


def test_structured_regeneration_is_metered(candidate, monkeypatch, caplog):
    client = GeminiClient()
    texts = iter(['not json', '{"supported":true}'])

    def post(*args, **kwargs):
        return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": next(texts)}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}

    monkeypatch.setattr(client, "_post", post)

    @track_query_usage
    def query():
        return client.generate_structured("Question", {"type": "object"}, operation="verify")

    with caplog.at_level(logging.INFO):
        assert query() == {"supported": True}
    detail = next(r for r in caplog.records if r.message == "query_generation_usage")
    assert detail.generation_calls == 2 and detail.prompt_tokens == 20
    assert any(e["code"] == "structured_regeneration" for e in detail.pipeline_events)
