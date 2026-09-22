"""Run four paid, synthetic-only requests through the extended shadow observers."""
import argparse
import json
import logging
import os
from pathlib import Path

from jev_settings import FEATURES
from jev_shadow import answer_policy, document_policy, history_selection
from token_usage import track_query_usage


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.decisions = []

    def emit(self, record):
        if record.msg == "jev_evaluation":
            self.calls.append({key: getattr(record, key) for key in
                               ("operation", "succeeded", "duration_ms", "estimated_cost_usd")})
        elif record.msg == "jev_policy_decision":
            fields = ("operation", "mode", "query_id", "baseline_route", "proposed_route", "baseline_planning",
                      "proposed_planning", "probabilities", "probability", "disagreement", "baseline_indices",
                      "proposed_indices", "baseline_profile", "proposed_profile", "proposed_instrument_type",
                      "proposed_review_pages", "sampled_pages", "origin")
            self.decisions.append({key: getattr(record, key) for key in fields if hasattr(record, key)})


@track_query_usage
def synthetic_queries():
    answer_policy("What is the permit fee?", "[1] The permit fee is INR 100.", "",
                  baseline_route="answer_direct", baseline_planning=False)
    answer_policy("Which fee applies today after the amendment?",
                  "[1] The 2020 rule sets INR 100. [2] Effective 2026-01-01, the amended fee is INR 200.", "",
                  baseline_route="answer_complex_high", baseline_planning=False)
    messages = [{"role": "user", "content": "Explain the timber permit."},
                {"role": "assistant", "content": "Timber needs a permit."},
                {"role": "user", "content": "Tell me a joke about gardens."},
                {"role": "assistant", "content": "A synthetic unrelated joke."},
                {"role": "user", "content": "Back to timber, which form?"},
                {"role": "assistant", "content": "Use Form A."}]
    history_selection(messages, "List the documents for that permit.", messages)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", required=True, action="store_true")
    parser.add_argument("--output", default="docs/jev_shadow_evaluation.json")
    args = parser.parse_args()
    for feature in FEATURES:
        os.environ[f"JEV_{feature.upper()}_MODE"] = "shadow"
    os.environ["RAG_HISTORY_SELECTION_TOKENS"] = "1"
    capture = Capture()
    for name in ("services.jev", "jev_policy"):
        logging.getLogger(name).addHandler(capture)
        logging.getLogger(name).setLevel(logging.INFO)
    synthetic_queries()
    document_policy({"title": "Synthetic permit procedure", "source": "synthetic.txt",
                     "metadata": {"document_type": "procedure"}, "pages": [
                         {"page": 1, "text": "Step 1: Submit Form A. Step 2: Pay INR 100. Step 3: Obtain approval."},
                         {"page": 2, "text": "x9q@@ !! 11lI0O xx zqv %%\ufffd\ufffd\ufffd --- 9 9 1 0"}]},
                    baseline_profile="procedure", document_ref="synthetic", origin="smoke")
    report = {"scope": "Synthetic transport and observation smoke test; not accuracy or savings validation",
              "calls": capture.calls, "decisions": capture.decisions,
              "all_calls_succeeded": len(capture.calls) == 4 and all(c["succeeded"] for c in capture.calls)}
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["all_calls_succeeded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
