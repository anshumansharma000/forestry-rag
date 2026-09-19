"""Bounded paid synthetic comparison: python -m scripts.evaluate_evidence_policy --live.

Toggles only this round's three flags; prior cost optimizations remain enabled.
No production documents or conversation history are sent.
"""
import argparse
import json
import os
from pathlib import Path

import prompts
from chunking import count_tokens
from retrieval import diversify_contexts
from scripts.evaluate_cost import MeteredClient

SOURCE = ("[1] Synthetic rules: Bamboo permits are fee-exempt but still require approval before transport. "
          "Submit Form A with proof of ownership to the District Forest Officer.")
AUDITS = [
    ("paraphrase", "How do I apply for a bamboo permit?",
     "Apply to the District Forest Officer using Form A and proof of ownership. "
     "No fee is payable for bamboo, but approval is required before transport [1]."),
    ("repair_exception", "How do I apply for a bamboo permit?",
     "Submit Form A with proof of ownership to the District Forest Officer. "
     "Bamboo is fee-exempt and may be transported without approval [1]."),
    ("unsupported", "What is the penalty for late renewal?", "The penalty is INR 500 [1]."),
]
FLAGS = ("RAG_SELECTIVE_EVIDENCE", "RAG_SELECTIVE_REASONING", "RAG_SELECTIVE_VERIFICATION")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--output", default="docs/evidence_policy_evaluation.json")
    args = parser.parse_args()
    results = []
    for optimized in (False, True):
        for flag in FLAGS:
            os.environ[flag] = str(optimized).lower()
        os.environ["RAG_COST_OPTIMIZATIONS"] = "true"
        for name, question, draft in AUDITS:
            client = MeteredClient()
            audits = []

            def structured(*args, _client=client, _audits=audits, **kwargs):
                result = _client.generate_structured(*args, **kwargs)
                _audits.append({"operation": kwargs.get("operation"), "result": result})
                return result

            prompts.generate_structured_with_gemini = structured
            try:
                answer = prompts.verify_answer_with_gemini(question, draft, SOURCE)
                results.append({"case": name, "optimized": optimized, "answer": answer,
                                "audits": audits, "calls": client.calls})
            except Exception as exc:
                results.append({"case": name, "optimized": optimized, "error_type": type(exc).__name__})
        context = {"document_id": "synthetic", "source": "synthetic.txt", "score": .9, "base_score": .9,
                   "text": ("Effective from 2024-01-01. This amendment sets the application fee at INR 100. "
                            "Bamboo remains fee-exempt but requires approval."),
                   "metadata": {"effective_date": "2024-01-01", "amendment_references": ["Amends the fee"]}}
        client = MeteredClient()
        prompts.generate_with_gemini = client.generate
        question = "What is the application fee in this amendment?"
        try:
            answer = prompts.answer_with_gemini(question, [context])
            results.append({"case": "amendment_lookup", "optimized": optimized, "answer": answer, "calls": client.calls})
        except Exception as exc:
            results.append({"case": "amendment_lookup", "optimized": optimized, "error_type": type(exc).__name__})
        candidates = [{**context, "id": i, "section_heading": "Fees", "chunk_index": i} for i in range(4)]
        candidates.append({**candidates[0], "id": 4, "text": "Approval must precede transport."})
        selected = diversify_contexts(candidates, 5)
        results.append({"case": "duplicate_selection", "optimized": optimized,
                        "selected_ids": [c["id"] for c in selected],
                        "formatted_source_tokens": count_tokens(prompts.format_contexts(selected)),
                        "evidence_tokens": sum(count_tokens(c["text"]) for c in selected)})
    Path(args.output).write_text(json.dumps(results, indent=2))
    for result in results:
        print(json.dumps({"case": result["case"], "optimized": result["optimized"],
                          "operations": [c["operation"] for c in result.get("calls", [])],
                          "cost_inr": sum(c.get("estimated_cost_inr") or 0 for c in result.get("calls", [])),
                          "error_type": result.get("error_type"), "evidence_tokens": result.get("evidence_tokens")}))


if __name__ == "__main__":
    main()
