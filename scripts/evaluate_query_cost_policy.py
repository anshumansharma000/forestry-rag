"""Offline-only historical reconciliation and scripted orchestration comparison.

Run: python -m scripts.evaluate_query_cost_policy --output engineering/query-cost/offline-report.json
No live option exists. Provider transports are blocked; tokens/answers/scores in the
candidate comparison are fixtures, not measured quality or production savings.
"""

import argparse
import json
import logging
import os
from collections import defaultdict
from datetime import date
from pathlib import Path
from unittest.mock import patch

import jev_shadow
import prompts
from chunking import count_tokens
from generation_cost import embedding_cost, generation_cost
from jev_settings import FEATURES
from services.gemini import GeminiClient
from token_usage import record_decision, record_embedding, track_query_usage

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 10, 17)


def historical_report(events):
    grouped = defaultdict(list)
    for item in events:
        grouped[item["query_id"]].append(item)
    queries = []
    for query_id, records in grouped.items():
        # Receipt events are charges; summaries are reconciliation targets, never additions.
        calls = [r for r in records if r["event"] in ("gemini_generation_received", "jev_evaluation")]
        summaries = [r for r in records if r["event"] == "query_answer_cost"]
        if len(summaries) > 1:
            raise ValueError("Duplicate completion summary")
        summary = summaries[0] if summaries else None
        known = sum(r["estimated_cost_inr"] for r in calls if r.get("estimated_cost_inr") is not None)
        expected = summary["known_api_cost_inr"] if summary else None
        if expected is not None and abs(known - expected) > 1e-9:
            raise ValueError(f"Historical cost does not reconcile: {query_id}")
        generation = [r for r in calls if r["event"] == "gemini_generation_received"]
        usage = next((r for r in records if r["event"] == "query_generation_usage"), {})
        queries.append({"query_id": query_id, "completion_recorded": summary is not None,
                        "outcome": summary["query_outcome"] if summary else "interrupted_or_missing_completion",
                        "known_api_cost_inr": known, "logged_known_api_cost_inr": expected,
                        "total_api_cost_inr": summary.get("total_api_cost_inr") if summary else None,
                        "embedding_cost_inr": usage.get("embedding_cost_inr"),
                        "generation_operations": [r["operation"] for r in generation],
                        "reconciled": abs(known - expected) < 1e-9 if expected is not None else None})
    return queries


class Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        if record.getMessage() == "query_generation_usage":
            self.rows.append(record)


def scripted_case(case, candidate):
    """Real prompt/routing/audit orchestration with entirely scripted provider responses."""
    configuration = {f"JEV_{feature.upper()}_MODE": "off" for feature in FEATURES}
    configuration.update({
        "RAG_ROUTING_POLICY": "jev-led-v1" if candidate else "legacy",
        "RAG_AUDIT_POLICY": "independent-v1" if candidate else "legacy",
        "RAG_EVIDENCE_COMPACTION": "exact-v1" if candidate else "legacy",
        "RAG_QUERY_CONTEXT_V2": "false", "RAG_ROUTE_POLICY": "legacy", "RAG_VERIFICATION_POLICY": "legacy",
        "RAG_PRUNE_INEFFECTIVE_DECISIONS": "false", "JEV_ROUTING_MODE": "active" if candidate else "off",
        "RAG_LITE_FIRST": "true", "RAG_COST_OPTIMIZATIONS": "true", "RAG_RISK_BASED_VERIFICATION": "true",
        "RAG_EVIDENCE_PLANNING": "true", "RAG_ANSWER_VERIFICATION": "true", "RAG_VERIFICATION_ESCALATION": "true",
        "RAG_COMPACT_VERIFICATION": "true", "RAG_SELECTIVE_VERIFICATION": "true", "RAG_LITE_EXTRACTION": "true",
        "GEMINI_DIRECT_MODEL": "gemini-3.5-flash-lite", "GEMINI_COMPLEX_MODEL": "gemini-3.8-flash",
        "GEMINI_UTILITY_MODEL": "gemini-3.5-flash-lite", "GEMINI_VERIFICATION_MODEL": "gemini-3.5-flash-lite",
        "JEV_ROUTE_THRESHOLD": ".8", "JEV_ROUTE_MARGIN": ".2", "COST_USD_TO_INR": "90",
        "COST_EMBEDDING_RATE_MODEL": "", "COST_ORDINARY_TARGET_INR": "1.2", "COST_COMPLEX_TARGET_INR": "1.5",
    })
    handler = Records()
    usage_logger = logging.getLogger("token_usage")
    old_level = usage_logger.level
    usage_logger.setLevel(logging.INFO)
    usage_logger.addHandler(handler)
    client = GeminiClient()

    def provider(model, action, payload, **kwargs):
        operation = kwargs["operation"]
        if operation == "plan":
            text = json.dumps({"question_scope": case["question"], "central_answer": case["contexts"][0]["text"],
                               "themes": [], "conflicts": [], "unknowns": []})
        elif operation in ("verify", "verify_complex"):
            text = json.dumps({"supported": True, "unchanged": True, "answer": "", "issues": []})
        else:
            text = "The fee is INR 100. Bamboo is exempt but still requires approval [1]."
        input_tokens = count_tokens(json.dumps(payload, ensure_ascii=False))
        output_tokens = count_tokens(text)
        # Deliberately scripted thinking counts. These are not predictions of model behavior.
        thoughts = 800 if operation == "answer_complex_high" else (300 if model == "gemini-3.8-flash" else 0)
        return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}],
                "usageMetadata": {"promptTokenCount": input_tokens, "candidatesTokenCount": output_tokens,
                                  "thoughtsTokenCount": thoughts, "totalTokenCount": input_tokens + output_tokens + thoughts}}

    def evaluate(state, checks, **kwargs):
        record_decision(kwargs["operation"], "jev-fixture", {"input_tokens": count_tokens(json.dumps(state))},
                        elapsed_ms=0, succeeded=True)
        return {key: case["scores"].get(key, .5) for key in checks}

    def blocked(*args, **kwargs):
        raise AssertionError("Network is forbidden in the offline evaluator")

    @track_query_usage
    def query():
        record_embedding("gemini-embedding-2", {"promptTokenCount": count_tokens(case["question"])})
        answer = prompts.answer_with_gemini(case["question"], case["contexts"])
        return {"outcome": prompts.answer_outcome(answer)}

    try:
        with patch.dict(os.environ, configuration), patch.object(client, "_post", provider), \
             patch.object(prompts, "generate_with_gemini", client.generate), \
             patch.object(prompts, "generate_structured_with_gemini", client.generate_structured), \
             patch.object(jev_shadow, "evaluate", evaluate), patch.object(prompts, "retrieval_is_confident", return_value=True), \
             patch("services.gemini.pooled_post", blocked), patch("services.jev.pooled_post", blocked), \
             patch("token_usage.generation_cost", lambda model, usage: generation_cost(model, usage, as_of=AS_OF)), \
             patch("services.gemini.generation_cost", lambda model, usage: generation_cost(model, usage, as_of=AS_OF)), \
             patch("token_usage.embedding_cost", lambda model, usage: embedding_cost(model, usage, as_of=AS_OF)):
            query()
    finally:
        usage_logger.removeHandler(handler)
        usage_logger.setLevel(old_level)
    record = handler.rows[0]
    return {"case": case["id"], "mode": "candidate" if candidate else "baseline",
            "operations": record.generation_operations, "audit_owner": record.pipeline_audit_owner,
            "simulated_api_cost_inr": record.total_api_cost_inr, "input_tokens_estimate": record.prompt_tokens,
            "scripted_output_tokens": record.output_tokens, "scripted_thinking_tokens": record.thinking_tokens,
            "compaction": [e for e in record.pipeline_events if e["code"] in ("evidence_compaction", "plan_compaction")],
            "decisions": [e for e in record.pipeline_events if e["code"] in ("route_decision", "independent_verification")],
            "target_status": record.api_cost_target_status}


def cases():
    row = {"document_id": "synthetic-one", "source": "synthetic-rules.txt", "section_heading": "Fees",
           "text": "The fee is INR 100. Bamboo is exempt but still requires approval.", "score": .9,
           "metadata": {"temporal_metadata_version": 1, "amendment_references": ["Synthetic amendment reference"]}}
    return [
        {"id": "amended_fact_lookup", "question": "What is the fee?", "contexts": [row],
         "scores": {"bounded": .95, "complex": .1, "reconciliation": .05}},
        {"id": "medium_direct_audit_gap", "question": "Explain the permit costs and requirements",
         "contexts": [{**row, "metadata": {}, "section_heading": f"Section {i}"} for i in range(11)],
         "scores": {"bounded": .1, "complex": .95, "reconciliation": .05}},
        {"id": "confirmed_conflict", "question": "Which rule applies to this project?",
         "contexts": [{**row, "metadata": {"legal_review": {"confirmed_conflict": True}}}],
         "scores": {"bounded": .95, "complex": .1, "reconciliation": .05}},
        {"id": "contained_evidence_overview", "question": "Give an overview of the fee requirements",
         "contexts": [{**row, "text": row["text"] * 8}, {**row, "text": row["text"] * 9}],
         "scores": {"bounded": .95, "complex": .1, "reconciliation": .05}},
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "engineering/query-cost/offline-report.json")
    args = parser.parse_args()
    fixture = json.loads((ROOT / "tests/fixtures/query_cost_logs.json").read_text())
    report = {"scope": "Offline only. Historical subtotals reconcile supplied logs. Candidate answers, scores, thinking and "
              "token usage are scripted fixtures; no demonstrated quality parity or live savings.",
              "pricing_as_of": AS_OF.isoformat(), "historical": historical_report(fixture["events"]),
              "scripted_comparison": [scripted_case(case, candidate) for case in cases() for candidate in (False, True)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"Offline report: {args.output}")


if __name__ == "__main__":
    main()
