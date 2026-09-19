# Token efficiency, model routing, and quality validation

The subsequent [cost policy](cost_policy.md) adds selective planning/history and per-stage INR cost accounting.

Routine factual lookups use `gemini-3.5-flash-lite`. Procedures, overviews, comparisons, and evidence requiring synthesis
use `gemini-3.8-flash`; amendment, historical, conflict, and precedence questions retain high thinking. The complex model
remains configurable through `GEMINI_COMPLEX_MODEL`. No database migration or reindex is required.

## Changes

- Fixed incidental keyword routing: “How much is the application fee?”, “How long is it valid?”, and
  “Who approves the application?” no longer trigger the three-stage procedural pipeline just because of “how” or
  “application”. Actual application workflows, mixed workflow questions, and comparisons retain their richer pipeline.
- Clear factual lookups do not escalate solely because retrieval returned several documents. Explicit temporal/conflict
  questions and amendment metadata still take precedence over this optimization.
- Replaced repetitive answer instructions with a shorter prompt retaining source boundaries, legal/temporal applicability,
  exceptions, uncertainty, citations, readable formatting, and evidence-grounded follow-ups.
- Direct lookups prefer 1–3 sentences, usually under 120 words. Broader answers usually target 200–450 words. These are
  soft targets, not truncation limits: completeness and qualifications take priority, and explicit detailed requests
  have no word target. Hard output limits and thinking settings remain unchanged.
- Exact repeated excerpt text within the same document/source, section, and issue/effective dates is referenced once.
  All source numbers, page labels, and dates remain available. Different documents, dates, sections, or even small text
  changes remain separate. No fuzzy deduplication or clause truncation is introduced.
- A first chat message skips rewriting. Follow-ups retain model-based rewriting and recent history. The latest question
  appears once in the answer prompt; evidence plans omit pretty-print indentation without removing fields.
- `RAG_COMPACT_VERIFICATION=true` returns a compact approval for an unchanged draft. Corrections return a complete answer;
  verification preserves Markdown layout and brevity. Set it to `false` to restore full-answer verification output.
- `RAG_VERIFICATION_ESCALATION=true` re-audits a rejected or inconsistent Lite verdict once with Gemini 3.8 Flash using
  all evidence. This handles false Lite rejections without bypassing verification. A second rejection still abstains.
  API errors do not trigger this extra call. Set the flag to `false` to disable escalation. Escalation adds cost only on
  the rejection path; structured-output parsing and transport retain their separately bounded retries.

## Measured smoke test

[Raw paired responses and usage](token_efficiency_smoke.json) use six synthetic questions and a fixed small evidence set.
The baseline is the local code immediately before this optimization round, including the earlier compact-verification
change. Both variants use the requested model split: Lite for utilities/direct answers and 3.8 Flash for complex answers.
These measurements do not include retrieval embeddings or reproduce production history and document sizes.

For the three routine lookups (fee, duration, approving authority), aggregate provider-reported generation usage was:

| Metric | Before | After | Reduction |
|---|---:|---:|---:|
| Input tokens | 6,745 | 2,469 | 63.4% |
| Output tokens excluding thinking | 1,210 | 121 | 90.0% |
| Thinking tokens | 1,111 | 0 | 100% |
| Total tokens including thinking | 9,066 | 2,590 | 71.4% |

Those queries previously made three generation calls each; they now make one Lite call. Reviewed final answers retained
core facts, the fee exemption, the validity/non-renewal condition, and the approving authority. An early prompt iteration
added an unstated payment recipient, which led to stronger grounding and narrower scope instructions before the final run.

Procedure/comparison/amendment probes retain complex routing. Their results showed reduced input but variable reasoning
usage and occasional verifier abstentions, including baseline abstentions. Repeated probes motivated the bounded stronger
audit; do not treat their small-sample totals as stable production savings. The separate
[escalation smoke test](token_efficiency_escalation_smoke.json) forces a Lite rejection and runs a real 3.8 Flash audit.

This is a smoke test, not proof of production-wide quality parity. A user's 10k input / 2k output query can have a different
mix of history, evidence, planning, drafting, verification, and thinking; applying these percentages to it is not guaranteed.

## Observability

`query_generation_usage` aggregates received generation responses for `/ask`, chat, and RAG Lab questions. It records
operations and models. Individual `gemini_generation_received` events share the `query_id` and include provider input,
output, thinking, cached, and total counts. Usage is recorded before malformed, empty, or truncated responses are rejected.
`answer_route_selected` adds question shape, selected operation, context count, planning status, and estimated source,
history, and answer-prompt sizes. `answer_verification_rejected` shows which audit rejected the draft.

Prompts and answers are not logged. Missing provider counts remain null. Cached tokens are a subset of input tokens;
do not add them again. Logs cannot recover provider usage for a network timeout returning no metadata and exclude
embeddings. Local `cl100k_base` estimates are not exact Gemini billing counts. Reconcile actual costs with provider billing.

## Validation and remaining limits

Run `.venv/bin/python -m pytest -q`. Regression tests cover factual/workflow routing, high-risk escalation, requested
model profiles, evidence preservation, duplicate provenance, citations, temporal rules, follow-up history, compact audit
outcomes, bounded verification escalation, and concurrent query accounting.

Before claiming production parity, compare a representative held-out corpus of questions and documents across repeated
runs. Review factual support, missing exceptions, dates, citation correctness, completeness, formatting, and inappropriate
abstention. Compare token/cost distributions by route, including escalation rates, rather than only overall averages.

The selective cost policy retains history verbatim when relevant and skips planning only for small single-source
procedures. Retrieval budgets remain unchanged for each shape, and complex planning/verification is retained. The oversized-first-chunk budget exception remains: silently chopping legal text
could discard a qualification. These can still make evidence-heavy or long-history queries expensive.
