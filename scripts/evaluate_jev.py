"""Paid, synthetic-only Jev smoke evaluation; does not access the document corpus."""
import argparse
import json
import logging
import os
from pathlib import Path

import jev_policy
from prompts import format_contexts


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.calls = []

    def emit(self, record):
        if record.msg == "jev_evaluation":
            self.calls.append({key: getattr(record, key) for key in
                               ("operation", "succeeded", "duration_ms", "estimated_cost_usd")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", required=True, action="store_true")
    parser.add_argument("--output", default="docs/jev_evaluation.json")
    args = parser.parse_args()
    for feature in ("VERIFICATION", "RERANK", "REWRITE"):
        os.environ[f"JEV_{feature}_MODE"] = "active"
    capture = Capture()
    decisions = []
    original_evaluate = jev_policy.evaluate

    def evaluate(*args, **kwargs):
        result = original_evaluate(*args, **kwargs)
        decisions.append({"operation": kwargs["operation"], "probabilities": result})
        return result

    jev_policy.evaluate = evaluate
    logging.getLogger("services.jev").addHandler(capture)
    logging.getLogger("services.jev").setLevel(logging.INFO)
    context = {"id": "synthetic", "document_id": "synthetic", "source": "synthetic.txt",
               "metadata": {}, "section_heading": "Application", "text":
               "Submit Form A and proof of ownership to the District Forest Officer. "
               "Pay INR 1,000 and obtain approval before transporting timber.", "score": .9}
    results = []
    for name, draft, expected in [
        ("supported", "Submit Form A and proof of ownership to the District Forest Officer. "
         "Pay INR 1,000 and obtain approval before transporting timber [1].", True),
        ("wrong_fee", "Submit Form A and proof of ownership to the District Forest Officer. "
         "Pay INR 100 and obtain approval before transporting timber [1].", False),
        ("missing_approval", "Submit Form A and proof of ownership and pay INR 1,000. "
         "You may then transport timber immediately [1].", False),
    ]:
        actual = jev_policy.approve_draft("List the application steps for a timber transit permit.",
                                         draft, [context], format_contexts([context]))
        results.append({"case": name, "expected": expected, "actual": actual, "passed": actual == expected})
    history = [{"role": "user", "content": "Only consider bamboo in Delhi as of 2020."},
               {"role": "assistant", "content": "I will use that scope."}]
    for name, latest, expected in [
        ("dependent", "What documents do I need for that?", False),
        ("inherited_scope", "What is the application fee?", False),
        ("standalone", "New topic: what is the timber permit fee in Mumbai in 2026?", True),
    ]:
        actual = jev_policy.standalone_question(history, latest)
        results.append({"case": name, "expected": expected, "actual": actual, "passed": actual == expected})
    candidates = [context, {**context, "id": "noise", "text": "The department has an office garden."},
                  {**context, "id": "exception", "text": "Bamboo is exempt from the fee but still requires approval."}]
    ordered = jev_policy.rerank("What fee and approval requirements apply to bamboo transport?", candidates)
    actual = [c["id"] for c in ordered]
    results.append({"case": "exception_promotion", "actual": actual,
                    "passed": actual == ["synthetic", "exception", "noise"]})
    report = {"scope": "Single-run synthetic smoke checks, not production quality or savings validation",
              "results": results, "calls": capture.calls, "decisions": decisions,
              "all_calls_succeeded": len(capture.calls) == 7 and all(c["succeeded"] for c in capture.calls)}
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["all_calls_succeeded"] and all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
