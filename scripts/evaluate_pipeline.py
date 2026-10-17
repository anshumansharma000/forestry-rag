"""Versioned synthetic replay and bounded opt-in model evaluation.

Replay exercises real orchestration/retrieval policy with deterministic fake model
outputs. It measures programmed work, not model quality or production savings.
Live mode uses supplied local evidence and models, never the configured corpus DB.
"""

import argparse
import hashlib
import json
import logging
import math
import os
import statistics
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from generation_cost import generation_cost, positive_setting
from pipeline_settings import PipelineSettings, policy_digest
from pipeline_trace import event
from query_cache import access_scope
from query_context import QueryContext, resolve_combined, use_context
from token_usage import track_query_usage

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/answer_pipeline/cases.json"
FEATURES = {
    "baseline": {},
    "context": {"RAG_QUERY_CONTEXT_V2": "true", "RAG_CONTEXT_RESOLVER": "combined"},
    "decisions": {"RAG_PRUNE_INEFFECTIVE_DECISIONS": "true"},
    "evidence": {"RAG_EVIDENCE_POLICY": "v2", "RAG_ADAPTIVE_RETRIEVAL": "true"},
    "planning": {"RAG_PLANNING_POLICY": "v2"},
    "routing": {"RAG_ROUTE_POLICY": "v2"},
    "verification": {"RAG_VERIFICATION_POLICY": "v2"},
    "transport": {"RAG_RETRIEVAL_CONCURRENCY": "2", "RAG_BATCH_NEIGHBORS": "true"},
}
BASE_ENV = {
    "RAG_QUERY_CONTEXT_V2": "false",
    "RAG_CONTEXT_RESOLVER": "legacy",
    "RAG_PRUNE_INEFFECTIVE_DECISIONS": "false",
    "RAG_EVIDENCE_POLICY": "legacy",
    "RAG_ADAPTIVE_RETRIEVAL": "false",
    "RAG_VERIFICATION_POLICY": "legacy",
    "RAG_PLANNING_POLICY": "legacy",
    "RAG_ROUTE_POLICY": "legacy",
    "RAG_LITE_FIRST": "false",
    "RAG_RETRIEVAL_CONCURRENCY": "1",
    "RAG_BATCH_NEIGHBORS": "false",
    "RAG_HTTP_POOLING": "false",
    "RAG_OPTIONAL_FOLLOWUP": "false",
    "RAG_EMBEDDING_CACHE": "false",
    "RAG_RETRIEVAL_CACHE": "false",
    "RAG_ANSWER_CACHE": "false",
    "RAG_LEGAL_HIERARCHY": "false",
    "RAG_LITE_EXTRACTION": "false",
    "RAG_RISK_BASED_VERIFICATION": "false",
    "RAG_COST_OPTIMIZATIONS": "true",
    "RAG_EVIDENCE_PLANNING": "true",
    "RAG_ANSWER_VERIFICATION": "true",
    "RAG_COMPACT_VERIFICATION": "true",
    "RAG_SELECTIVE_PLANNING": "true",
    "RAG_SELECTIVE_HISTORY": "true",
    "RAG_SELECTIVE_EVIDENCE": "true",
    "RAG_SELECTIVE_REASONING": "true",
    "RAG_SELECTIVE_VERIFICATION": "true",
    "RAG_VERIFICATION_ESCALATION": "true",
    "GEMINI_GENERATION_MAX_RETRIES": "0",
    "GEMINI_EMBEDDING_MAX_RETRIES": "0",
    "JEV_SHADOW_SAMPLE_RATE": "1",
}
COMBINED = {k: v for name, settings in FEATURES.items() if name != "baseline" for k, v in settings.items()}
FEATURES["combined"] = COMBINED


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, requests, tokens, usd):
        self.max_requests, self.max_tokens, self.max_usd = requests, tokens, usd
        self.requests = self.tokens = 0
        self.usd = 0.0
        self.incomplete = False

    def before(self, payload, model):
        # Conservative reservation for text-only calls, using repository rate card.
        # A price ceiling is an estimate, never a provider-enforced billing limit.
        upper_input = len(json.dumps(payload).encode()) + 4096
        upper_output = payload.get("generationConfig", {}).get("maxOutputTokens", 0)
        upper_cost = (
            generation_cost(model, {"promptTokenCount": upper_input, "candidatesTokenCount": upper_output, "thoughtsTokenCount": 0})[
                "estimated_cost_usd"
            ]
            if upper_output
            else self.embedding_cost(model, upper_input)
        )
        if self.incomplete or upper_cost is None or self.requests >= self.max_requests:
            raise BudgetExceeded("request/price completeness limit")
        if self.tokens + upper_input + upper_output > self.max_tokens or self.usd + upper_cost > self.max_usd:
            raise BudgetExceeded("conservative token/spend reservation limit")
        self.requests += 1

    @staticmethod
    def embedding_cost(model, tokens):
        rate = positive_setting("COST_EMBEDDING_USD_PER_MILLION_TOKENS", 0)
        if type(tokens) is not int or tokens < 0 or rate is None or os.getenv("COST_EMBEDDING_RATE_MODEL") != model:
            return None
        return tokens * rate / 1_000_000

    def after(self, model, action, data):
        usage = data.get("usageMetadata") or {}
        total = usage.get("totalTokenCount", usage.get("promptTokenCount"))
        cost = (
            generation_cost(model, usage)["estimated_cost_usd"]
            if action == "generateContent"
            else self.embedding_cost(model, usage.get("promptTokenCount"))
        )
        if type(total) is not int or cost is None:
            self.incomplete = True
        else:
            self.tokens += total
            self.usd += cost


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        if record.msg == "query_generation_usage":
            names = (
                "generation_calls",
                "generation_operations",
                "embedding_calls",
                "decision_calls",
                "total_api_cost_usd",
                "total_api_cost_complete",
                "estimated_cost_usd",
                "prompt_tokens",
                "output_tokens",
                "thinking_tokens",
                "pipeline_events",
                "pipeline_audit_owner",
            )
            self.records.append({name: getattr(record, name, None) for name in names})


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * p) - 1)]


def summarize(results):
    summary = {}
    for mode in sorted({r["mode"] for r in results}):
        rows = [r for r in results if r["mode"] == mode]
        latencies = [r["duration_ms"] for r in rows]
        costs = [r["usage"].get("total_api_cost_usd") for r in rows if r.get("usage")]
        summary[mode] = {
            "requests": len(rows),
            "errors": sum(bool(r.get("error")) for r in rows),
            "p50_ms": statistics.median(latencies),
            "p95_ms": percentile(latencies, 0.95),
            "known_cost_fraction": sum(c is not None for c in costs) / len(rows),
            "p50_api_usd": statistics.median(costs) if len(costs) == len(rows) and all(c is not None for c in costs) else None,
            "observed_fact_check_passes": sum(r.get("checks_passed") is True for r in rows),
            "expert_quality": "not_measured",
            "false_abstention_proxy": sum(r.get("false_abstention", False) for r in rows),
            "false_answer_proxy": sum(r.get("false_answer", False) for r in rows),
            "forbidden_claim_proxy": sum(r.get("forbidden_claim_proxy", False) for r in rows),
            "mean_required_evidence_recall": statistics.mean(
                r["required_evidence_recall"] for r in rows if r.get("required_evidence_recall") is not None
            )
            if any(r.get("required_evidence_recall") is not None for r in rows)
            else None,
        }
    return summary


@track_query_usage
def run_case(case, mode, *, live=False, budget=None):
    import prompts
    import retrieval
    from conversation_context import rewrite_question_for_retrieval, select_history, should_select_history
    from services.gemini import GeminiClient

    class LocalRepository:
        # Synthetic local evidence only; this adapter never opens Supabase.
        def match_chunks(self, vector, query, limit):
            terms = retrieval.meaningful_terms(query)
            rows = []
            for i, doc in enumerate(case["evidence"]):
                overlap = len(terms & retrieval.meaningful_terms(doc["text"]))
                if not overlap:
                    continue
                rows.append(
                    {
                        "id": f"chunk-{i}",
                        "document_id": doc["id"],
                        "source": doc["id"] + ".txt",
                        "chunk_index": 0,
                        "chunk_type": "section",
                        "section_heading": "Synthetic provision",
                        "page_start": 1,
                        "page_end": 1,
                        "content": doc["text"],
                        "metadata": doc.get("metadata", {}),
                        "similarity": 0.6 + min(0.3, overlap * 0.03),
                    }
                )
            return sorted(rows, key=lambda r: r["similarity"], reverse=True)[:limit]

    calls = []

    def generate(prompt, **kwargs):
        operation = kwargs.get("operation", "answer_direct")
        calls.append(operation)
        event("replay_generation", operation=operation)
        return case.get("resolved_question", case["question"]) if operation == "rewrite" else case["replay_answer"]

    def structured(prompt, schema, **kwargs):
        op = kwargs["operation"]
        calls.append(op)
        event("replay_generation", operation=op)
        if op == "rewrite_context":
            return {
                "query": case.get("resolved_question", case["question"]),
                "can_reduce_history": False,
                "assistant_indices": [],
                "keep_assistant_indices": [],
                "unresolved": [],
                "constraints": [],
            }
        if op == "plan":
            return {"central_answer": case["replay_answer"], "conflicts": [], "unknowns": []}
        return {"supported": True, "unchanged": True, "answer": "", "issues": [], "issue_codes": []}

    class BoundedClient(GeminiClient):
        def _post(self, model, action, payload, **kwargs):
            budget.before(payload, model)
            try:
                data = super()._post(model, action, payload, max_retries=0, **kwargs)
            except Exception:
                budget.incomplete = True
                raise
            budget.after(model, action, data)
            return data

    with ExitStack() as stack:
        if live:
            client = BoundedClient()
            stack.enter_context(patch.object(prompts, "generate_with_gemini", client.generate))
            stack.enter_context(patch.object(prompts, "generate_structured_with_gemini", client.generate_structured))
            stack.enter_context(patch.object(retrieval, "gemini_client", client))
        else:
            stack.enter_context(patch.object(prompts, "generate_with_gemini", generate))
            stack.enter_context(patch.object(prompts, "generate_structured_with_gemini", structured))
            stack.enter_context(patch.object(retrieval, "generate_structured_with_gemini", structured))
            stack.enter_context(patch.object(retrieval, "embed_query", lambda _: [1.0]))
            stack.enter_context(patch.object(retrieval, "embed_queries", lambda qs: [[1.0] for _ in qs]))
        history = case.get("history", [])
        if PipelineSettings.read().context_v2 and PipelineSettings.read().resolver == "combined":
            context = resolve_combined(history, case["question"])
        else:
            if should_select_history(history):
                resolved, selected = select_history(history, case["question"])
            else:
                resolved, selected = rewrite_question_for_retrieval(history, case["question"]), history
            context = QueryContext.create(case["question"], resolved=resolved, history=selected)
        with use_context(context if PipelineSettings.read().context_v2 else None), access_scope("offline-evaluation"):
            contexts = retrieval.retrieve(context.resolved_question, repository=LocalRepository())
            answer = prompts.answer_with_gemini(case["question"], contexts, context.messages())
        return {
            "answer": answer,
            "outcome": prompts.answer_outcome(answer),
            "selected_source_ids": [c["document_id"] for c in contexts],
            "replay_operations": calls,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=FIXTURES)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--modes", nargs="+", choices=list(FEATURES), default=list(FEATURES))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-requests", type=int, default=0)
    parser.add_argument("--max-tokens", type=int, default=0)
    parser.add_argument("--max-usd", type=float, default=0)
    args = parser.parse_args(argv)
    if not 1 <= args.repeats <= 10:
        parser.error("repeats must be 1–10")
    if args.live and (args.max_requests <= 0 or args.max_tokens <= 0 or not math.isfinite(args.max_usd) or args.max_usd <= 0):
        parser.error("live mode requires positive request, token and estimated USD ceilings")
    dataset = json.loads(args.dataset.read_text())
    capture = LogCapture()
    logger = logging.getLogger("token_usage")
    logger.addHandler(capture)
    old_level = logger.level
    logger.setLevel(logging.INFO)
    budget = Budget(args.max_requests, args.max_tokens, args.max_usd)
    report = {
        "scope": "live fixed-local-evidence evaluation" if args.live else "deterministic synthetic policy replay",
        "limitations": (
            "No production traffic, no SQL retrieval benchmark, no expert legal-quality labels; " "replay timings are not model latency."
        ),
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "results": [],
        "policies": {},
    }
    stopped = False
    try:
        from jev_settings import FEATURES as JEV_FEATURES

        base = {**BASE_ENV, **{f"JEV_{f.upper()}_MODE": "off" for f in JEV_FEATURES}}
        for mode in args.modes:
            with patch.dict(os.environ, {**base, **FEATURES[mode]}):
                report["policies"][mode] = policy_digest()
                for case in dataset["cases"]:
                    for repeat in range(args.repeats):
                        result = {"case": case["id"], "category": case["category"], "mode": mode, "repeat": repeat}
                        started = time.perf_counter()
                        before = len(capture.records)
                        try:
                            result.update(run_case(case, mode, live=args.live, budget=budget))
                            result["checks_passed"] = all(x.lower() in result["answer"].lower() for x in case.get("required_terms", []))
                            result["forbidden_claim_proxy"] = any(
                                x.lower() in result["answer"].lower() for x in case.get("forbidden_terms", [])
                            )
                            expected = case.get("expected_outcome")
                            result["false_abstention"] = expected == "answered" and result["outcome"] != "answered"
                            result["false_answer"] = expected == "insufficient_evidence" and result["outcome"] == "answered"
                            required = set(case.get("required_source_ids", []))
                            result["required_evidence_recall"] = (
                                len(required & set(result["selected_source_ids"])) / len(required) if required else None
                            )
                        except BudgetExceeded:
                            result["error"] = "budget_exhausted"
                            stopped = True
                        except Exception as exc:
                            result["error"] = type(exc).__name__
                        result["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
                        result["usage"] = capture.records[-1] if len(capture.records) > before else None
                        report["results"].append(result)
                        report["summary"] = summarize(report["results"])
                        report["by_category"] = {
                            category: summarize([r for r in report["results"] if r["category"] == category])
                            for category in sorted({r["category"] for r in report["results"]})
                        }
                        args.output.parent.mkdir(parents=True, exist_ok=True)
                        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
                        if stopped:
                            return 2
    finally:
        logger.removeHandler(capture)
        logger.setLevel(old_level)
    print(json.dumps({"scope": report["scope"], "cases": len(report["results"]), "output": str(args.output)}))
    return 1 if any(r.get("error") for r in report["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
