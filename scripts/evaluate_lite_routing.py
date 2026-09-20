"""Small paid synthetic-only comparison; explicitly run with --live."""
import argparse
import json
import os
from pathlib import Path

import prompts
from scripts.evaluate_cost import CONTEXT, MeteredClient

PLAIN = {**CONTEXT, "text": "Submit Form A and proof of ownership to the District Forest Officer. "
         "Pay INR 1,000 and obtain approval before transporting timber. Permits are valid for 30 days and cannot be renewed."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', required=True, action='store_true')
    parser.add_argument('--output', default='docs/lite_routing_evaluation.json')
    args = parser.parse_args()
    results = []
    for enabled in (False, True):
        os.environ['RAG_LITE_EXTRACTION'] = str(enabled).lower()
        os.environ['RAG_RISK_BASED_VERIFICATION'] = str(enabled).lower()
        os.environ['RAG_COST_OPTIMIZATIONS'] = 'true'
        for question in ['How do I apply for a timber transit permit?', 'List the application steps for a timber transit permit.']:
            client = MeteredClient()
            prompts.generate_with_gemini = client.generate
            prompts.generate_structured_with_gemini = client.generate_structured
            record = {'optimized': enabled, 'question': question,
                      'route': prompts.answer_generation_operation(question, [PLAIN])}
            try:
                record['answer'] = prompts.answer_with_gemini(question, [PLAIN])
                text = record['answer'].lower()
                record['checks'] = {word: word in text for word in ['form a', 'ownership', 'approval', '[1]']}
            except Exception as exc:
                record['error_type'] = type(exc).__name__
            record['calls'] = client.calls
            costs = [call['estimated_cost_usd'] for call in client.calls]
            record['estimated_generation_cost_usd'] = sum(costs) if costs and all(c is not None for c in costs) else None
            results.append(record)
            Path(args.output).write_text(json.dumps(results, indent=2))
            print(json.dumps({key: value for key, value in record.items() if key not in ('answer', 'calls')}), flush=True)


if __name__ == '__main__':
    main()
