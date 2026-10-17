# Query cost policies — 17 October 2026

The change gives active Jev routing authority to downgrade or promote a draft, separates
audit strength from drafting strength, and removes exact repeated evidence where its
provenance matches. Public answer response shapes are unchanged. The local `.env`, live
data, deployment configuration and provider models were not changed for this work.

## Policy and behavior

Three independent settings default to `legacy`: `RAG_ROUTING_POLICY=jev-led-v1`,
`RAG_AUDIT_POLICY=independent-v1`, and `RAG_EVIDENCE_COMPACTION=exact-v1` opt into the
new behaviors. These controls are independent of the older `RAG_COST_OPTIMIZATIONS`
master flag. Use [candidate.env](candidate.env) as an evaluation manifest and
[rollback.env](rollback.env) to restore legacy answer behavior. Nothing applies either
manifest automatically. Do not deploy on the basis of offline results.

With routing active, the existing combined Jev call scores bounded extraction/synthesis,
complex synthesis and legal reconciliation. A category needs score >= 0.80 and a >= 0.20
margin; both thresholds are configurable. They are decision thresholds, not calibrated
accuracy probabilities. Active Jev can lower an old high route. Confirmed evidence or
plan conflicts, explicit applicability reconciliation and material judicial interpretation
override a cheaper proposal. Dates, amendment metadata, evidence size and unresolved intent
alone do not force high reasoning. Confident Jev reconciliation can select high; an
uncertain/unavailable routing decision uses Lite for demonstrably bounded evidence, Flash
medium otherwise, with high reserved for those safeguards. Missing evidence stays missing.

`JEV_ROUTING_MODE=shadow` records the candidate decision but preserves baseline drafting.
`off` preserves baseline drafting and makes no routing call. A later plan conflict can
override the selected route without another Jev call. Routing technical failures use the
new conservative fallback regardless of `JEV_ACTIVE_FAILURE_POLICY`; that existing flag
still controls eligible Jev verification failures. Routing uses the resolved question,
original request and retained history from the existing shared context. No new resolver
call was introduced. This context is also enabled by either new routing or audit policy.

Independent auditing covers every substantive factual answer, including the previous
medium-Flash direct-answer audit gap. Lite audits bounded support even if Flash drafted
the answer. Actual reconciliation tasks, confirmed conflicts or reconciliation claims in
the draft get Flash auditing directly. High drafting alone does not require a Flash audit.
The conservative English/Hindi task/claim patterns are safeguards, not a complete semantic
legal-risk detector; broader language coverage and false negatives need live reviewed cases.
Existing narrow Jev audit eligibility remains; an active negative Jev verdict is final.
Technical Gemini audit failures abstain; substantive Lite audit rejection permits at most
one strong audit when existing escalation is enabled. Quotes and citations remain checked.

Strong audits now have separate `GEMINI_VERIFY_COMPLEX_MAX_OUTPUT_TOKENS=4000` and
`GEMINI_VERIFY_COMPLEX_THINKING_LEVEL=medium` controls. Previously they inherited complex
draft settings. Explicitly configure these if a deployment depended on a complex-draft
override for auditing. Draft output allowances and truncation recovery are unchanged.

Evidence formatting builds one canonical serialization reused by routing, planning,
drafting and auditing. Exact contained passages can reference the complete containing
passage only with matching document, source, section and temporal/version/scope metadata.
All public citation numbers remain valid; original source payloads stay intact. Material
uncited qualifications remain in audit evidence. Long literal evidence copies in a plan
can become source references; generated conclusions, conflicts and unknowns remain.
Before/after estimates are logged. A fallback retains the original full serialization
when compaction would increase its estimated size. Provider input is still billed at
each call; serializing evidence once is not provider-side caching.

## Accounting and diagnostics

`query_generation_usage` retains generation-only fields and adds raw/normalized per-call
usage, timing, generation attempt, output allowance, thinking setting, per-operation
transport attempt counts, unmetered attempt records and independent all-API target status.
`query_answer_cost` remains one completion summary. Never sum it with stage receipts.
Targets default to INR 1.20 ordinary / 1.50 complex, include all priced query API calls,
and do not abort work. A query using complex drafting or strong auditing is classified
complex for this telemetry. Incomplete totals have target status `unknown`, never
`within_target`. The previous generation-only `cost_exceeded` field stays compatible.

Missing thinking is inferred only from compatible complete prompt/output/total counts;
raw provider metadata is retained. Unknown pricing, missing usage and failed unmetered
attempts prevent a complete total. Synchronous text `gemini-embedding-2` pricing is
USD 0.20/M tokens, verified on 17 October 2026 at
[Google pricing](https://ai.google.dev/gemini-api/docs/pricing). Synchronous
`batchEmbedContents` uses Standard pricing, not the asynchronous Batch discount.
Explicit custom embedding rates must bind the model. No character-count token guesses
or retrospective replacement of historical unknown embedding bills are made. FX remains
the configured estimate; tax, infrastructure and ingestion allocation are excluded.

Trace/log decisions expose owner, version, scores, baseline/proposed/applied route,
fallback reason, safeguards and independent audit reason without prompts or source text.
The cache recipe was bumped; policy settings, thresholds, model identifiers and token
allowances participate in cache identity. Credentials remain excluded.

## Offline evidence and remaining gate

[offline-report.json](offline-report.json) reconciles five answered query subtotals:
INR 0.4927869, 0.96157296, 2.25857304, 3.390633 and 4.53627342. All five historical
embedding totals remain unknown. Three failed pre-provider queries have zero recorded
charges. The interrupted request has INR 0.5865894 in known generation/decision receipts
and no completion record; it is not treated as a zero-cost or completed answer.

The report's baseline/candidate comparisons run real orchestration with scripted answers,
Jev scores and token/thinking counts. They exercise amendment downgrade, the added audit,
confirmed-conflict protection and evidence compaction. Their costs are simulations, not
production savings or evidence of model-quality parity. Quality fixes can increase cost.

Reproduce without paid calls:

```sh
TIKTOKEN_CACHE_DIR=/private/tmp/answer-audit-tiktoken .venv/bin/python -m pytest -q
TIKTOKEN_CACHE_DIR=/private/tmp/answer-audit-tiktoken .venv/bin/python -m scripts.evaluate_query_cost_policy
```

The evaluator has no live option and blocks provider transports. Tests use scripted
providers; opt-in database/Redis integration suites remain disabled unless separately
requested. No migration is needed for this change.

Before promotion, recover the original expensive questions, conversation scope and evidence
snapshots. Logs alone cannot establish that their high routes were unnecessary. Obtain a
separate paid-evaluation budget and reviewed labels; vary routing and audit independently,
then evaluate their combination. Measure all-API mean/p50/p95 cost, overruns, unknown costs,
unsupported claims, missing conditions, applicability errors, false abstentions, audit
escalations and latency. Reject promotion on a critical legal-quality regression even if
cost decreases. A confirmed INR 1.50 ceiling has not been demonstrated.
