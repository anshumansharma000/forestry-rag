# Cost policy and rollout

`RAG_COST_OPTIMIZATIONS=true` enables selective planning and selection of verbatim conversation history. The model split
remains Gemini 3.5 Flash-Lite for direct answers/utilities and Gemini 3.8 Flash for complex answers. High-risk reasoning,
retrieval evidence, output ceilings, citations, full-evidence verification, and bounded verification escalation remain.

## Selective planning

A procedural question can omit the separate evidence-plan call only if its retrieved evidence is from one known
source/document, has at most four excerpts and 2,500 estimated formatted-source tokens, and has no high-risk temporal,
amendment, or conflict route. Explicit requests for detailed, exhaustive, or all provisions retain planning. Overviews,
comparisons, larger evidence sets, and multi-document procedures retain it as well. The answer still uses Flash and is
verified against every excerpt. `RAG_SELECTIVE_PLANNING=false` restores unconditional planning for broad question shapes.

## Conversation history

When the recent ten-message window exceeds `RAG_HISTORY_SELECTION_TOKENS` (default 1,500) and contains at least two
assistant replies, a structured Lite call combines the existing retrieval rewrite with history selection. It does not add
an ordinary extra model stage. All user messages, the latest assistant message, and relevant/uncertain earlier assistant
messages stay verbatim and in order. Only unrelated older assistant replies may be omitted from the answer prompt.
There is no persisted rolling summary, so omissions cannot accumulate between turns. An uncertain selection or invalid
indices retain the full recent window. An unusable query falls back to the existing rewrite, which can add a call.

The selector still reads the full recent window, so this saves its second transmission to the answer model, not its first
transmission to the rewrite model. A conversation where every prior reply matters will not get this saving. Set
`RAG_SELECTIVE_HISTORY=false` to retain the previous full-history behavior.

## Cost measurement

`gemini_generation_received` now includes estimated USD/INR cost for each response. `query_generation_usage` aggregates
those costs, reports each stage/model, counts verification escalations, and flags `cost_exceeded` against
`COST_QUERY_MAX_INR` (default 1.5). This is advisory, not an enforced spending cap: it never cuts evidence or returns an
incomplete answer just to meet a price target.

The estimator uses the published standard text-generation rates verified on September 17, 2026. It prices uncached input,
cached input, and output plus thinking separately. The announced January 1, 2027 increase for Flash 3.8 is date-aware.
Unknown models, non-standard service tiers, missing usage, or unmetered transport failures yield unknown total costs,
not zero. Individual known stages remain visible. Clean, explicit verification approvals that echo the exact unchanged
draft no longer trigger a redundant escalation; contradictory audits remain rejected.

`COST_USD_TO_INR=90` is an illustrative conversion, not a live exchange rate. Configure it to match billing FX. These
estimates exclude taxes, infrastructure, embedding calls, cache storage, and other services. They are not invoices.
Rate cards must be maintained if Google changes prices. See [Google pricing](https://ai.google.dev/gemini-api/docs/pricing).

## Verify deployment and rollback

The runtime configuration and query logs include `cost_policy_version=selective-evidence-audit-2026-09-18` and the policy flags.
Check them in the deployed service before assuming new traffic uses these changes. Local edits do not deploy the service.
The sample environments and Render configuration enable the policy; explicit production environment overrides prevail.

Set `RAG_COST_OPTIMIZATIONS=false` to disable both selective behaviors while retaining usage/cost telemetry. Individual
flags allow a narrower rollback. The existing planning, verification, and escalation switches are unchanged.

## Evaluation

Run regression tests with `.venv/bin/python -m pytest -q`. The suite covers retained exceptions and citations, procedure
planning eligibility, historical/conflict guards, verbatim corrections and references, uncertain selection fallback,
model-specific costs, cached/thinking accounting, future rates, missing usage, and advisory cost thresholds.

Run the bounded live synthetic comparison with:

```sh
.venv/bin/python -m scripts.evaluate_cost --live --output docs/cost_evaluation.json
```

This makes paid calls on five synthetic cases in two modes. It uses fixed evidence, not production retrieval. The modes
differ in the selective policy only; baseline drafting and verification otherwise remain the same. The JSON includes
answers, selections, simple fact-presence checks, calls, actual provider token counts, and estimated cost. Those checks
and a small sample do not establish production-wide quality equivalence. Review the answers, not only token totals.
Before claiming production savings, evaluate a held-out sample of real queries across repeated runs, including disputes,
exceptions, historical applicability, ambiguous references, and prior-answer corrections. Monitor p50/p95 cost, unsupported
claims, missing qualifications, false abstentions, and escalation frequency. Do not enforce a hard monetary cutoff that
silently sacrifices answer completeness.

### Recorded final comparison

The final [paired report](cost_evaluation.json) preserves actual responses, provider usage and stage costs. The
[initial report](cost_evaluation_initial.json) is retained to show the iteration where history selection was too conservative.
The final selector removed only the unrelated assistant replies and retained the earlier permit wording and all user constraints.

| Targeted case | Baseline generation cost | Optimized generation cost | Reduction |
|---|---:|---:|---:|
| Small single-document procedure | ₹0.390 | ₹0.296 | 24.1% |
| Procedure with a fee exemption | ₹0.948 | ₹0.764 | 19.4% |
| Procedure following a long chat | ₹1.082 | ₹0.682 | 37.0% |

The long-chat case used 9,888 versus 5,495 input tokens (44.4% fewer). These are additional savings against the previous
cost policy, using the same model split and full evidence. They use the illustrative ₹90/USD conversion and exclude taxes
and other services. Each number is one stochastic run; reasoning and escalation varied between runs, including in an
overview control whose planning was unchanged. Do not extrapolate these numbers to all production requests.

All four substantive answer scenarios passed their listed fact-presence checks and manual review of the requested core
facts, exceptions and citations. The exact-earlier-quote scenario abstained in both baseline and optimized modes despite
retaining the referenced message; this is an existing behavior, not demonstrated quality parity for quoted-history tasks.
The selector's correctness and numeric cost-accounting tests are deterministic, but model-quality equivalence still needs
a broader production-like evaluation. The policy does not guarantee that every complex query costs less than ₹1.50.

## Evidence, reasoning and verification refinement (18 September 2026)

Three independently reversible policies are enabled by default. Setting the master
`RAG_COST_OPTIMIZATIONS=false` disables them along with the preceding optimizations.

- `RAG_SELECTIVE_EVIDENCE`: drop only verbatim duplicate/contained text in the same
  known document, source, section and date context. Do not refill unused slots with
  these duplicates. Similar wording with different numbers, negation, dates or
  provenance remains eligible; fuzzy similarity no longer defers such evidence.
  Direct matches retain priority. Remaining neighbor slots favor qualifications,
  exceptions and amendments before generic adjacent context. This can make more
  bounded neighbor database reads (one per eligible anchor), but adds no model calls.
  Whole chunks and existing budgets remain; the first oversized clause is not cut.
- `RAG_SELECTIVE_REASONING`: narrow factual lookups in one small, explicitly dated,
  already-effective amendment excerpt use **Gemini 3.8 Flash medium** instead of
  high. Unknown/future dates, multiple excerpts/amendment references, historical,
  current-applicability, conflict, repeal and supersession questions retain high
  reasoning. Routine routing to Gemini 3.5 Flash Lite is unchanged. No output cap
  was lowered and no existing verification stage was removed.
- `RAG_SELECTIVE_VERIFICATION`: decide support for the *final repaired answer*,
  distinguish faithful paraphrases from unsupported claims, and repair material
  omissions or incorrect conditions instead of immediately abstaining. The compact
  schema places decisions before answer text and describes their meaning. Full
  evidence auditing, strict approval parsing, citation validation and the bounded
  stronger-model escalation remain. Unrelated facts and optional follow-up style
  are not reasons to reject an otherwise supported answer.

### Bounded live comparison

Run `python -m scripts.evaluate_evidence_policy --live` to repeat the paid,
synthetic-only evaluation. It toggles just this round's three flags, leaving earlier
cost optimizations enabled. Raw synthetic audit decisions, provider usage and costs
are saved in `docs/evidence_policy_evaluation.json`.

| Case | Before | After | Observation |
| --- | ---: | ---: | --- |
| Repair wrong bamboo approval condition, verification only | ₹0.2490 | ₹0.0311 | About 88% lower; Lite repaired it without escalation |
| Dated amendment fee lookup, generation only | ₹0.2735 | ₹0.1858 | About 32% lower; both preserved INR 100 and the bamboo exemption/approval condition |
| Valid paraphrase, verification only | ₹0.0202 | ₹0.0203 | Essentially unchanged; both approved |
| Unsupported renewal penalty, verification only | ₹0.1143 | ₹0.1489 | Cost increased; both correctly abstained after escalation |
| Duplicate-heavy formatted source block | 226 tokens | 103 tokens | About 54% fewer estimated input tokens, retaining both distinct clauses |

These are one paired run per synthetic case, not production averages or guaranteed
savings. The selection case is deterministic and uses the local tokenizer, including
source headers and the pre-existing duplicate aliases; it is not provider-billed
usage. INR estimates use the configured illustrative ₹90/USD and exclude taxes,
embeddings and infrastructure. The new audit instructions add input tokens; savings
come when compact approvals or fewer unnecessary escalations offset that overhead.
Evidence deduplication only saves where qualifying overlap exists, and retaining
legally distinct near-duplicates may increase evidence volume compared with fuzzy
filtering. Quality checks covered paraphrases, correction of a dangerous exemption
claim, unsupported-answer abstention, and preservation of the fee/exception in
medium reasoning. A representative labeled production evaluation is still needed to
estimate false-rejection rates and confirm aggregate quality/cost improvements.

Validation: 207 automated tests pass, including 18 new evidence/routing/verification
boundary checks. Changes are local and have not been deployed.

## Truncation recovery (20 September 2026)

A supplied production trace showed `answer_complex_high` ending with `MAX_TOKENS`:
3,843 thinking tokens plus 153 visible answer tokens under a 4,000-token ceiling.
Rewrite and planning completed; verification never ran. The query's generation
cost was approximately INR 2.056, of which INR 1.746 was the failed answer.

High-reasoning answers now use a separate
`GEMINI_COMPLEX_HIGH_MAX_OUTPUT_TOKENS=8192`; the medium profile remains at 4000.
The ceiling is for thinking plus visible output, not a requested answer length.
Existing complex-model overrides still apply. Existing deployments overriding only
`GEMINI_COMPLEX_MAX_OUTPUT_TOKENS` must use the new high-specific setting if they
want to override the high-reasoning budget too.

`GEMINI_TRUNCATION_RECOVERY=true` permits one regeneration per generation call,
only on `MAX_TOKENS`, at twice the previous ceiling, bounded by
`GEMINI_TRUNCATION_MAX_OUTPUT_TOKENS=16384`. It preserves the original prompt,
evidence, model, reasoning level, system instruction and response schema. It does
not resume or concatenate partial text, lower reasoning, bypass verification, or
retry an unchanged ceiling. Content-less truncated responses are handled as
truncation too. Repeated truncation still fails closed with diagnostic details.
HTTP retry and structured JSON parsing policies remain separate.

Every attempt is metered. Generation logs now include the effective ceiling,
thinking level and attempt number; routing logs include counts of amendment
contexts and plan conflicts without logging source text. Raising the ceiling does
not require the model to use it, but recovery and additional reasoning can increase
cost and latency. This is a reliability correction, not a cost-saving claim or a
hard INR 1.50 guarantee. The supplied trace lacks evidence/plan contents, so it
cannot establish whether high reasoning was necessary; that routing decision is
preserved pending evidence-based evaluation.

Validation: 261 tests passed, 4 skipped, including recovery from empty and partial
truncation, bounded repeated failure, retained evidence/schema/reasoning, usage
accounting for both attempts, opt-out, and no retry on success/upstream errors.
The actual PARIVESH request cannot be replayed from the trace alone because its
retrieved excerpts and history are absent. Deploy the updated backend and config,
then replay the original question against the same documents to confirm completion
and review its cost. These changes have not been deployed by this task.

## Selective Lite extraction and stronger legal audits (20 September 2026)

The new policy is opt-in (`RAG_LITE_EXTRACTION`, `RAG_RISK_BASED_VERIFICATION`,
`RAG_LEGAL_BATCH_EMBEDDINGS`). Code and deployment examples default these flags to false;
local development enables them explicitly. Existing truncation-recovery changes are preserved.
No database migration, annotation update, or frontend change is required.

Lite eligibility is deterministic: an explicit extraction/summary/checklist request or simple
application procedure, one identified source, at most four excerpts and 2,000 excerpt tokens.
Empty evidence, unresolved review gaps, draft legal profiles, amendments, judicial material,
exceptions/exemptions, cross-instrument conditions, and applicability/temporal/conflict questions
are excluded. Follow-ups with supplied history do not take the new drafting shortcut. This
intentionally does not move amendment reporting or multi-document comparisons to Lite yet.

Eligible extraction skips the planning call, drafts on Lite, and **always** receives the full-evidence
Lite audit, even when ordinary answer verification is disabled. A rejected audit retains the existing
bounded Flash escalation. The master cost flag disables the new Lite shortcut. Set
`RAG_LITE_EXTRACTION=false` to roll it back independently.

With `RAG_RISK_BASED_VERIFICATION=true`, high-reasoning answers receive a direct Flash audit,
including direct questions that previously skipped auditing because of their question shape.
Judicial/current-applicability questions and retrieved judicial profiles also take the high route.
This safety policy can increase cost on difficult questions. It is independent of the cost master
switch, and can be reverted with its own flag. Planned Flash audits are counted separately from
actual Lite-to-Flash escalations.

Legal retrieval still executes all six category and legacy searches. After the handbook step,
five remaining embeddings are requested in one batch. This reduces embedding HTTP requests from
six to two before relationship expansion; it does **not** imply fewer billed input tokens or fewer
SQL searches. Provenance-aware exact containment deduplication runs before final context packing,
and identical legal annotations are represented once per document/profile in the prompt. No source
excerpts, dates, exceptions, or amendment checks are truncated to save cost.

The admin legal preview now has aggregate request accounting. Existing generation-only cost fields
remain unchanged. New fields include embedding calls/items, `embedding_cost_usd/inr`,
`total_api_cost_usd/inr`, and `total_api_cost_complete`. Embedding costs require provider-reported
`usageMetadata.promptTokenCount`, `COST_EMBEDDING_RATE_MODEL` matching the actual model, and a verified
`COST_EMBEDDING_USD_PER_MILLION_TOKENS` value. Missing metadata, pricing, or failed attempts produce
unknown totals, not zero. No extra token-count API is called. Embeddings without usage metadata
cannot be fully priced from these logs; reconcile with provider billing. Infrastructure/taxes remain
outside the estimate, and the existing advisory threshold remains generation-only.

### Live synthetic checks

The [initial paired report](lite_routing_evaluation_initial.json) revealed a weakness: Lite instructed
an exempt bamboo applicant to pay the applicable fee before stating the exemption, and the Lite audit
accepted it. The eligibility rule was tightened to exclude exception-bearing procedures. The report
is retained rather than counting this as a successful quality comparison.

The [subsequent paired report](lite_routing_evaluation.json) used an unconditional synthetic timber
procedure. Generation plus verification estimates were:

| Wording | Previous policy | Lite policy |
|---|---:|---:|
| How do I apply? | $0.0051204 | $0.0008660 |
| List the application steps | $0.0028965 | $0.0007428 |

These are individual stochastic runs of closely related questions on the same synthetic source,
not independent production benchmarks. Form, ownership, fee, and approval appeared in the outputs;
wording, explicit timing, and optional detail varied. Presence checks alone do not establish legal
quality equivalence. A broader SME-reviewed evaluation is still required before production enablement.
Run `python -m scripts.evaluate_lite_routing --live` to repeat this bounded paid comparison.
