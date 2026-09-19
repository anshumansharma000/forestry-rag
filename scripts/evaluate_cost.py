"""Run with: python -m scripts.evaluate_cost --live --output docs/cost_evaluation.json.

Only synthetic evidence is sent. Baseline disables the new selective planning/history
policy; both modes retain the same models, evidence, answer limits, and verification.
"""

import argparse
import json
import logging
import os
from pathlib import Path

import conversation_context
import prompts
from generation_cost import generation_cost
from services.gemini import GeminiClient

EVIDENCE = (
    "Synthetic Green State rules, effective 1 January 2024: A timber transit permit costs INR 1,000. "
    "Bamboo permits are fee-exempt but still require approval. To apply, submit Form A with proof of ownership "
    "to the District Forest Officer, pay the applicable fee, and obtain approval before transport. "
    "Permits are valid for 30 days and cannot be renewed."
)
CONTEXT = {"source": "synthetic-rules.txt", "document_id": "synthetic-one", "chunk_index": 0,
           "text": EVIDENCE, "score": .9, "base_score": .9, "evidence_role": "matched"}
HISTORY = [
    {"role": "user", "content": "Use Green State rules as of 2024 for my timber transit permit. Answer in English."},
    {"role": "assistant", "content": "First condition: Submit Form A with proof of ownership. Second: Obtain approval before transport."},
    {"role": "user", "content": "Separately, tell me about nursery inspections."},
    {"role": "assistant", "content": "Nursery inspection notes are unrelated to transit permit applications. " * 150},
    {"role": "user", "content": "Now explain biodiversity surveys."},
    {"role": "assistant", "content": "Biodiversity surveys record species and habitats; this topic is separate from permits. " * 150},
    {"role": "user", "content": "Return to my timber transit permit under those earlier rules."},
    {"role": "assistant", "content": "Returning to your timber transit permit under Green State rules as of 2024."},
]

CASES = [
    {"id": "small_procedure", "question": "How do I apply for a timber transit permit?", "contexts": [CONTEXT],
     "checks": ["form a", "ownership", "district forest officer", "1,000", "approval"]},
    {"id": "fee_exception", "question": "How do I apply for a bamboo permit?", "contexts": [CONTEXT],
     "checks": ["form a", "exempt", "approval"]},
    {"id": "overview", "question": "What are the provisions for timber transit permits?", "contexts": [CONTEXT],
     "checks": ["form a", "1,000", "30", "renew"]},
    {"id": "long_chat", "question": "How do I apply, and what exception affects the fee?",
     "contexts": [CONTEXT], "history": HISTORY, "checks": ["form a", "ownership", "1,000", "bamboo", "exempt"]},
    {"id": "earlier_quote", "question": "Quote the first condition you listed earlier exactly.",
     "contexts": [CONTEXT], "history": HISTORY, "checks": ["submit form a with proof of ownership"]},
]


class MeteredClient(GeminiClient):
    def __init__(self):
        super().__init__()
        self.calls = []

    def _post(self, model, action, payload, **kwargs):
        data = super()._post(model, action, payload, **kwargs)
        usage = data.get("usageMetadata", {})
        self.calls.append({"operation": kwargs.get("operation"), "model": model, "usage": usage,
                           **generation_cost(model, usage)})
        return data


def run_case(case, optimized):
    os.environ["RAG_COST_OPTIMIZATIONS"] = str(optimized).lower()
    client = MeteredClient()
    prompts.generate_with_gemini = client.generate
    prompts.generate_structured_with_gemini = client.generate_structured
    conversation_context.generate_structured_with_gemini = client.generate_structured
    result = {"case": case["id"], "mode": "optimized" if optimized else "baseline"}
    history = case.get("history", [])
    try:
        if conversation_context.should_select_history(history):
            query, selected = conversation_context.select_history(history, case["question"])
        else:
            query = prompts.rewrite_question_for_retrieval(history, case["question"]) if history else case["question"]
            selected = history
        answer = prompts.answer_with_gemini(case["question"], case["contexts"], selected)
        result.update({"answer": answer, "resolved_query": query,
                       "selected_history_indices": [i for i, message in enumerate(history) if message in selected],
                       "checks": {term: term.lower() in answer.lower() for term in case["checks"]}})
    except Exception as exc:
        # No request URL, credentials, or exception diagnostics in the report.
        result["error_type"] = type(exc).__name__
    result["calls"] = client.calls
    result["totals"] = {
        name: sum(call["usage"].get(name, 0) for call in client.calls)
        for name in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "totalTokenCount")
    }
    result["estimated_cost_inr"] = (
        sum(call["estimated_cost_inr"] for call in client.calls)
        if all(call["estimated_cost_inr"] is not None for call in client.calls) else None
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Authorize paid model calls on synthetic data")
    parser.add_argument("--output", type=Path, default=Path("docs/cost_evaluation.json"))
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to run the bounded paid evaluation")
    logging.disable(logging.CRITICAL)
    os.environ.update({"GEMINI_GENERATION_MAX_RETRIES": "0", "RAG_SELECTIVE_PLANNING": "true",
                       "RAG_SELECTIVE_HISTORY": "true", "RAG_EVIDENCE_PLANNING": "true",
                       "RAG_ANSWER_VERIFICATION": "true", "RAG_COMPACT_VERIFICATION": "true",
                       "RAG_VERIFICATION_ESCALATION": "true", "GEMINI_DIRECT_MODEL": "gemini-3.5-flash-lite",
                       "GEMINI_COMPLEX_MODEL": "gemini-3.8-flash", "GEMINI_UTILITY_MODEL": "gemini-3.5-flash-lite",
                       "GEMINI_VERIFICATION_MODEL": "gemini-3.5-flash-lite"})
    report = {"scope": "Synthetic fixed-evidence smoke test; one sample per mode, not production quality parity.",
              "cases": CASES, "results": []}
    for case in CASES:
        for optimized in (False, True):
            result = run_case(case, optimized)
            report["results"].append(result)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({key: result.get(key) for key in ("case", "mode", "totals", "estimated_cost_inr", "checks")}), flush=True)


if __name__ == "__main__":
    main()
