# Implementation handoff: answer quality, performance, and cost — priorities 1–6

Prepared 15 October 2026. Baseline commit: `6d2cf3a971f9299b217dd29a4341aa8393e807ee`.

## Mandate and scope

Implement the first six priorities agreed in this chat: consistent question interpretation; better evidence selection and targeted extraction quality; removal of unnecessary decision/planning work; better verification; caching and duplicate-work prevention; and database/network efficiency.

The objective is a lower total cost per correctly resolved question with improved answer quality and response time. Preserve material qualifications, source provenance, temporal applicability, access controls, and atomic chat/index operations. This document specifies implementation and execution; it is not evidence that changes have been implemented or deployed.

Read these local artifacts first:

- [Current-system audit](/Users/anshumansharma/Documents/Playground/outputs/answer-generation-audit-2026-10-15/current-system.md)
- [Code inventory, effective local profiles, and safe settings snapshot](/Users/anshumansharma/Documents/Playground/outputs/answer-generation-audit-2026-10-15/code-map.json)
- [Baseline test output](/Users/anshumansharma/Documents/Playground/outputs/answer-generation-audit-2026-10-15/verification.txt)

Baseline evidence: 387 tests passed, 16 opt-in PostgreSQL/Redis integration tests skipped. Local Jev modes are all active; source defaults are off; deployment templates use shadow. The actual deployed configuration is unknown. The saved Jev smoke evaluation passed five of seven behavioral checks despite seven successful API calls. Do not treat these artifacts as production accuracy or savings estimates.

Priorities 7–8 are outside this implementation: bulk ingestion embedding reuse/batching, an extraction cache, asynchronous provider batches, broad prompt rewrites, and explicit provider prompt caching. Targeted extraction repairs are part of priority 2. Removing the obligatory follow-up sentence is a small agreed part of priority 3. Do not introduce a new model provider, framework, fine-tuning, semantic answer cache, or global legal-hierarchy enablement.

## Working rules for the next agent

1. Recheck HEAD, status, applicable `AGENTS.md`, and source before editing. The audit may be older than the implementation checkout. Record meaningful drift.
2. Preserve existing staged spreadsheets and untracked cost-report outputs. Commit/stage only implementation-owned files if asked to commit. Do not reset the shared checkout. Use a `codex/` branch if creating a branch is appropriate.
3. Treat current application code as the compatibility baseline. Preserve public endpoint shapes, `rag.py` imports, outcome strings, source/citation numbering, chat leases, idempotency and atomic publication. Add internal contracts rather than forcing a public API migration.
4. Implement the code, synthetic fixtures, migration tests, benchmark tooling and rollback paths without waiting for production data. If representative labels or infrastructure are unavailable, finish the independent engineering work and clearly separate code readiness from rollout eligibility.
5. Prepare deployment changes and migrations as reviewable artifacts. This planning request does not itself authorize applying migrations to a shared/live database, reindexing production documents, changing live settings, or running an unbounded paid evaluation. Use existing authorization if supplied later; otherwise surface only the specific remaining operational decision after local work is complete.
6. Do not print secrets, raw environment files, private questions, document text, or cache values in routine logs. Use allowlisted settings, internal identifiers and content hashes. Diagnostic fixtures should be synthetic or explicitly approved.

## Execution order and deliverables

Priority ranking expresses user value. The order below expresses implementation dependencies.

| Step | Work package | Priority served | Depends on | Deliverable |
|---|---|---|---|---|
| A | Reproducible baseline, stage telemetry and evaluation harness | All | None | Comparable baseline and candidate measurements |
| B | Shared request context and bounded history access | 1, 6 | A | All answer stages use consistent scope |
| C | Decisions that can affect results; bounded planning policies | 3 | B | Fewer ineffective decisions, independently gated policy experiments |
| D | Evidence quality, scope isolation and targeted extraction | 2 | B, A | Better selected evidence with explicit coverage diagnostics |
| E | Verification policy and bounded repair | 4 | B, C, D | Audits matched to evidence/risk; measurable error categories |
| F | Versioned invalidation, caches and duplicate-work control | 5 | B, D, E | Safe reuse with fresh access checks and valid evidence |
| G | Batched database reads, bounded concurrency and connection reuse | 6 | A, D; coordinate with F | Faster equivalent retrieval with correct accounting |
| H | Full comparison, rollout manifests and handoff | All | A–G | Evidence-backed release/rollback decision per feature |

Use one focused change set per package, with separate commits if working in a commit-producing workflow. C's behavior-preserving pruning and G's transport work can land independently of experimental quality policies. Do not combine all behavioral changes into one unmeasurable switch.

Execution checklist (the implementation agent should annotate each item with checks and artifact locations):

- [ ] A — baseline and evaluation/telemetry harness
- [ ] B — shared context and recent-history access
- [ ] C — decision pruning and separately gated planning/routing
- [ ] D — scoped evidence selection and extraction pilot
- [ ] E — explicit verification and bounded repair
- [ ] F — invalidation, caches and duplicate-work handling
- [ ] G — equivalent batched/concurrent retrieval and connection reuse
- [ ] H — integrated validation and release/rollback package

## A. Establish measurement and a reproducible baseline

**Files:** `token_usage.py`, `generation_cost.py`, `structured_logging.py`, `settings.py`, `scripts/evaluate_*.py`, `tests/conftest.py`. Add a focused evaluation harness, for example `scripts/evaluate_pipeline.py`, and synthetic fixtures under `tests/fixtures/answer_pipeline/`.

### Implementation

- Snapshot baseline policy/configuration, corpus/revision identity, prompt versions, embedding configuration and model identifiers. Preserve baseline outputs before changing behavior. Explicitly set evaluation modes so local `.env` does not accidentally mix policies.
- Add stage timing and counters for context resolution, embedding, each retrieval group, reranking, evidence packing, planning, drafting, verification and cache activity. Distinguish attempted/returned calls, retries, JSON regeneration, truncation regeneration and semantic verification escalation.
- Record internal outcome and reason codes, selected evidence IDs, approximate full prompt size, removed redundant tokens, policy versions, actual stage calls and final verification owner. Continue avoiding raw prompt/source/answer logging.
- Preserve unknown costs as unknown. Add completeness and priced-coverage reporting. Configure embedding prices only from a verified current rate for the actual model; if unavailable, compare usage and billable calls without fabricating a complete total.
- Report p50/p95 latency and cost, supported-answer coverage, false abstention, unsupported claims, missing material conditions, citation correctness, applicability errors and retrieval evidence recall. Include failures and retries in the denominator and spending totals.
- Measure cold, warm and concurrent workloads separately. Cache-hit savings must not disguise a more expensive cold path. Do not sum stage durations as end-to-end latency once stages can run concurrently.
- Support baseline versus one-change-at-a-time comparisons and a final combined comparison. Store per-case results, configuration, corpus identity, usage and errors. Existing fixed-evidence scripts remain useful smoke checks but cannot substitute for retrieval evaluation.

### Evaluation set

Start with an explicitly versioned suite covering factual lookups, procedures with exceptions, comparisons/overviews, amendment/current/historical applicability, dependent follow-ups/corrections/quotations, and insufficient or misleading evidence. Include English/Hindi or mixed-language questions if representative of actual usage; current English regex routing and English full-text search require testing on that slice.

Create deterministic synthetic regression fixtures immediately. For rollout, target an initial 120 representative expert-labeled cases, approximately 20 per category above, with tuning and held-out cases split before tuning. Record this as an initial engineering sample, not statistical proof of rare-error safety. Add source IDs, required facts/qualifications, forbidden claims, expected scope and whether abstention is appropriate. Repeat stochastic candidate comparisons at least three times per case within the agreed live-run budget.

Live evaluation must be explicitly opt-in, have request/token/spend ceilings, stop before issuing additional calls when the ceiling is exhausted, and preserve partial results. Missing real labels should leave behavioral features off while the implementation and synthetic checks proceed.

**Acceptance:** Existing accounting tests pass; every entry path (`/ask`, chat, Lab, legal preview) produces a coherent trace. Tests explicitly cover both baseline modes and the local all-active combination; the current autouse defaults must not hide that combination.

## B. Shared request context and lean conversation access

**Files:** `chat_service.py`, `conversation_context.py`, `prompts.py`, `retrieval.py`, `routers/qa.py`, `routers/legal.py`, `rag_lab_service.py`, `repositories.py`, `schemas.py`. Suggested new module: `query_context.py`.

### Internal contract

Introduce an immutable, validated `QueryContext` with:

- `original_question` and `resolved_question`;
- known jurisdiction, as-of date, named documents, entities, exclusions and output/language requirements;
- references to the user/history spans establishing those constraints; retain exact quotation/numbered-list anchors;
- selected verbatim history, unresolved references and a resolution status;
- one request-scoped date value and resolution-policy version.

Unknown scope remains unknown. Explicit user corrections take precedence over older constraints. The resolver must not invent a jurisdiction, date, document identifier or legal fact. Do not make an unvalidated paraphrase the sole authority: retain original wording and source spans and fall back to the existing recent-history representation on invalid structured output.

Conversation history can establish user intent and what was previously said; prior assistant prose must not become authoritative legal evidence. Preserve that distinction in resolver, draft and verification prompts, especially for exact-earlier-quote cases.

### Implementation

1. Build trivial context without a model call for no-history requests. Merge legal-preview as-of/jurisdiction inputs explicitly rather than relying only on retrieved metadata.
2. Feed the resolved question and applicable constraints to retrieval, route/planning decisions, evidence planning, drafting and verification. Drafting also receives the original wording and necessary verbatim anchors. Risk detection considers both original and resolved scope; rewriting must not erase a high-risk signal.
3. Introduce keyword-only optional context arguments or thin adapters so existing `answer_with_gemini(question, contexts, history)` and facade imports remain compatible. Resolve once per request and pass the object; do not resolve independently in every helper.
4. Add `ChatRepository.get_recent_messages_for_answer(session_id, user_id, limit=10)`. Assert ownership, order descending with a stable tie-break, limit in the database, then restore chronological order. Fetch only required ID/role/content/order fields. Do not invoke legacy citation enrichment in this lean path. Preserve the existing public messages endpoint and its source enrichment.
5. Do not merely pass `limit=10` to the existing ascending query: that returns the oldest messages. If strict sequence is unavailable, define deterministic tie handling and add a database-backed test.
6. Implement a selectable combined resolver using the existing structured rewrite/history-selection machinery. In combined mode, do not also run Jev rewrite/history calls. Keep the legacy Jev ownership path as a selectable baseline for comparison.
7. Keep all user turns within the selected window and the latest assistant reply unless a validated policy establishes that they are unnecessary. A standalone-search verdict alone does not authorize dropping answer preferences or quote references. Long-term memory beyond ten messages is a separate capability; do not imply this change recovers constraints outside the window.

**Acceptance tests:** Inherited jurisdiction/date/negation; later correction overrides earlier scope; quoted prior text retained exactly; numbered references; explicit topic reset with persistent language preference; ambiguous resolution fallback; no-history zero resolver calls; exactly one resolver owner; planner and verifier see resolved constraints; no mutation of cached/shared objects; recent fetch selects latest ten; session ownership and completed-turn replay still happen before work.

**Rollout:** Enable the consistent context plumbing after regression comparison. Benchmark combined versus legacy resolution separately; do not change the default resolver owner without labeled results.

## C. Remove ineffective decisions and make added work earn its cost

**Files:** `jev_shadow.py`, `jev_policy.py`, `jev_settings.py`, `prompts.py`, `model_routing.py`, `settings.py`, corresponding routing/JeV tests.

### C1 — Behavior-preserving work pruning

- Compute whether each proposed decision can affect the answer before constructing its Jev question. A locked high-risk route cannot be downgraded or upgraded; omit its routing questions. Planning disabled explicitly means no planning question.
- Skip other planning questions only where the implemented guards prove both yes/no proposals leave the result unchanged. Retain a decision where either outcome could change behavior. Unit-test this truth table rather than approximating eligibility with a keyword.
- Routing/planning share a request: only skip the whole request when no effective question remains. Adjust parsers so they do not expect deliberately omitted keys. Missing unexpected keys remain malformed responses.
- Keep full shadow diagnostics available, but add deterministic sampling for optional shadow observations. Sample by a request identifier/hash before any model call, never based on outcomes; log selection and sampling rate. Active decisions required by the policy are not sampled away.
- Replace full candidate metadata in Jev reranking with an allowlist retaining source, section, dates, instrument/review status and material amendment/qualification information. Remove duplicated retrieval-fusion/debug fields. Compare on boundary fixtures before enabling.
- Make the closing follow-up sentence optional in both draft and verifier instructions. Keep it when it helps the user's next step; its absence must not cause rejection.

### C2 — Separately gated behavior experiments

- Add a versioned planning policy allowing direct drafting for small, coherent synthesis cases. Retain explicit detailed/comparison requests, unresolved conflicts, multi-instrument reconciliation and necessary applicability planning until evaluated.
- Add a versioned route policy that distinguishes relevant amendment reasoning from an incidental metadata mention. Preserve all exceptions/judicial/uncertain-applicability guards until the new evidence diagnostics can establish a safe alternative. Count and review every route downgrade in evaluation.
- Prefer existing deterministic routing for clear cases; introduce no additional classifier call merely to decide whether to call a classifier. Preserve a full-context audit on any new cheaper drafting route until quality evidence supports another policy.

**Acceptance:** Pruned cases make zero unnecessary calls and retain route/plan/answer behavior in deterministic tests. Effective decisions still run. Shadow sampling cannot alter answers. Experimental planning or route changes pass held-out quality gates and reduce measured total cost or latency, including downstream audits/retries.

## D. Evidence quality and targeted extraction improvements

**Files:** `retrieval.py`, `legal_retrieval.py`, `legal_registry.py`, `documents.py`, `chunking.py`, `ingest_service.py`, `rag_lab_service.py`, repository adapters and retrieval/quality tests. Suggested new module: `evidence_policy.py`.

### Scope contract first

Introduce an explicit `RetrievalScope` identifying ordinary corpus, legal preview namespace or immutable Lab revision, with the backend identity, access-scope digest, effective retrieval options, index/registry versions and request date/as-of scope. All caches and searches consume this scope.

The current global legal flag is checked before ordinary retrieval uses its supplied repository. Ensure a Lab query cannot accidentally search the ordinary corpus through a default legal registry. Keep Lab retrieval revision-bound, or implement a genuinely revision-bound legal adapter; never silently mix sources. Add a regression test for Lab plus global legal flag enabled.

### Evidence selection

1. Define a small deterministic evidence-requirements object from `QueryContext`: requested fact, procedure elements, paired comparison subjects, date/jurisdiction applicability and required amendment/exception checks. Avoid an unconditional new LLM planning call.
2. Track selected evidence roles and coverage diagnostics, including required qualifications, missing source relationships, conflicting dates, and omitted required passages. A role label or category match is not proof of correctness or legal completeness.
3. Preserve whole clauses/rows with headings and material conditions. Keep provenance-aware exact containment deduplication. Do not use fuzzy similarity to discard distinct legal provisions or summarize source passages with an LLM as a token-saving shortcut.
4. Protect relevant exceptions/amendments before filling optional neighbor slots. If a budget prevents sufficient coverage, retain an explicit gap. Preserve a bounded oversized-whole-clause policy rather than cutting away qualifications silently.
5. Add adaptive retrieval as an independent mode: retain batched facets for obviously broad questions; for eligible narrower questions, search once and expand only if coverage diagnostics justify it. Preserve existing mandatory identifier/update checks. At most one expansion round, within existing candidate/context ceilings; no open-ended retrieval loop.
6. Add a calibrated evidence-sufficiency policy using the labeled set. Do not globally raise `RETRIEVAL_CONFIDENCE_THRESHOLD` or present a ranking score as correctness probability. Until calibration is available, emit diagnostics and preserve the current behavior.
7. Measure the full formatted evidence/prompt overhead in addition to excerpt estimates. Provider usage remains the billing authority; local tokenizer estimates must be labeled as estimates.

### Extraction pilot within this scope

- Use representative synthetic and approved source files with tables, merged cells, repeated headers, numeric units, Hindi/mixed text and corrupt native PDF text.
- Compare the existing table-aware and native extraction paths against the original page. Preserve row/column context and section ancestry in chunk metadata/text where necessary.
- Add deterministic extraction-quality diagnostics for suspicious output; OCR should remain targeted and bounded. Low text quantity alone does not establish extraction quality, and a noisy heuristic must not reject valid Hindi or short pages.
- Store the extraction/chunk-format recipe version in metadata/fingerprint so affected files can be reprocessed deliberately. Existing indexed files must not be silently treated as newly improved.
- Produce an affected-document manifest and an unpublished Lab pilot. No whole-corpus reindex or promotion of inferred legal annotations to reviewed status. Publish only reviewed, validated revisions during the separately authorized rollout.

**Acceptance:** Required-evidence recall and material-condition coverage do not regress on the frozen suite; source IDs/pages and citation mapping remain stable; exact duplicate removal does not lose distinct provenance; broad/current/historical cases retain required checks; unknown metadata remains unknown; adaptive mode counts and explains expansions. Report quality and cost separately for the extraction pilot.

## E. Verification matched to risk and failure type

**Files:** `prompts.py`, `jev_policy.py`, `model_routing.py`, `token_usage.py`, `schemas.py` only if additive internal structures are placed there. Suggested module: `verification.py`.

### Policy

- Create an explicit `VerificationDecision` containing owner, required checks and reason codes. Inputs include resolved scope, evidence diagnostics, route changes and material claim risk. A short/direct question can still be consequential.
- Maintain deterministic citation-ID validation and exact quotation matching against the appropriate permitted text. A quotation match does not prove legal applicability, and presence of one valid citation does not establish claim coverage.
- Introduce a structured audit result with `supported`, `unchanged`, final answer and bounded issue codes: citation defect, unsupported claim, missing condition, scope mismatch, temporal/applicability conflict, insufficient evidence, invalid audit output or technical failure. Retain compatibility parsing for legacy responses.
- In the v2 policy, ordinary factual answers that lack a validated deterministic support path receive a lightweight semantic audit; high-risk applicability/conflict answers retain strong auditing. Clearly report any resulting additional cost. Cache only eligible semantically approved answers later.
- Mechanical checks may normalize unambiguous citation formatting. Do not guess a missing/out-of-range citation or keep its claim by stripping the bad reference.
- Use the existing Gemini audit's ability to return a repaired complete answer. Add at most one explicit repair step only where the audit identifies a repairable defect and the existing permitted escalation budget supports it. The final replacement must pass citation checks and a support audit covering all material claims and relevant qualifications. Do not audit only a faulty sentence while ignoring contradictions elsewhere.
- Missing evidence is not repairable by rewriting. Return a qualified supported answer where policy permits, or the existing abstention outcome. Keep public outcome strings stable; record richer reasons internally.

### Jev ownership boundary

Preserve the current active Jev negative-decision contract: rejection must not silently become another model attempt to obtain approval. Keep existing `closed` versus `baseline` technical-failure behavior. Evaluate Jev thresholds and verifier ownership offline by question type. If results support a different owner for a class, encode that as an explicit versioned policy chosen before running the verifier. Do not use lower thresholds simply to reduce refusals.

**Acceptance:** Supported answers are retained; unsupported numeric details, omitted exceptions, incorrect historical applicability and fabricated citations are caught; repair cannot invent facts; negative Jev verdicts do not trigger hidden escalation; technical failures remain distinguishable from semantic rejection; all repair/escalation paths are bounded and metered. Review false approvals and false abstentions separately.

## F. Safe reuse and duplicate-work control

**Files:** `retrieval.py`, `repositories.py`, `legal_registry.py`, `chat_service.py`, `routers/qa.py`, `rag_lab_repository.py`, `token_usage.py`, `settings.py`; new `query_cache.py`/`cache_keys.py`; additive SQL migration(s).

### Cache boundaries and initial limits

Use a cache interface with a disabled/no-op implementation and a Redis implementation; reuse the Redis technology already in the project with separate key namespaces and bounded memory/TTL. Cache availability must not bypass authentication or request admission. Cache errors bypass reuse; failures in required security limits retain their existing fail-closed behavior.

| Layer | Key components | Initial policy |
|---|---|---|
| Embedding | Exact prepared embedding text hash, actual model/version, dimensions, task/preprocessing version, backend/account scope | TTL 24 hours; dimensions and finite values validated on read; no corpus invalidation needed |
| Retrieval | Resolved query + scope/constraints, corpus/registry generation, access scope, backend/Lab revision, date, full effective retrieval/Jev policy and model version | TTL 5 minutes; read current generation before reuse; no partial/failed results |
| Answer | Original + resolved request and permitted output constraints, all retrieval key inputs, evidence/version identity, prompt/route/verifier/model versions | Implement opt-in for standalone `/ask`, same user/access scope, explicitly supported and audited non-temporal source-fact answers; TTL at most 5 minutes |

These TTLs are initial conservative operational defaults, not measured optimums. All cache layers ship off until their tests pass; answer caching stays off until the quality and isolation gate passes. For model aliases whose concrete version is not exposed, include an explicit deployment/cache epoch and invalidate on provider/model-policy updates. Do not lowercase, remove punctuation or otherwise merge semantically different questions for cache keys. HMAC-sensitive key material and never log raw query/cache payloads.

Implement exact answer caching within the requested scope, but restrict initial eligibility: no chat response replay through this general cache, no historical/current-applicability requests, no unresolved constraints, no abstentions, no errors, and no unverified direct drafts. Revalidate the cached structured schema/citations and current scope/version before returning it. Never reuse session IDs, message IDs, request IDs or a prior user's persisted response envelope.

### Transactional invalidation

Add a generation/version source for the live corpus and relevant legal registry namespace. Cover successful publication, direct live chunk/document edits or deletion, Lab publication into the corpus, and reviewed legal-profile changes. Use transactionally updated counters/triggers or publication hooks with tests proving every supported mutation path is covered. Staging-only chunks must not invalidate live retrieval. Namespace changes must invalidate both affected namespaces where applicable.

Do not infer corpus identity from maximum timestamp or a row count. Do not make invalidation best-effort after committing a publication. Read versions before work and recheck before cache publication; on a change, do not store a mixed-version result. At most one bounded recomputation if needed; otherwise bypass cache and surface the existing consistency/error handling rather than entering a retry loop.

On a cache hit, bind validation to a defined version-read point after authorization; recheck versions after obtaining the payload before accepting it. Do not claim that a cache guarantees perpetual freshness across a concurrent publication after that point. Preserve the baseline request consistency contract and test both sides of the publication boundary.

Use immutable Lab revision identity plus readiness/lifecycle checks. Ensure stale/deleted/disallowed revisions cannot be served by cached values. Unknown version capability or a missing migration means bypass the corresponding cache, not serve potentially stale data.

### Duplicate execution and accounting

- Preserve database chat request-ID replay and per-session leases. Document frontend retry-ID requirements; the frontend is absent here, so do not claim to have changed it.
- Coalesce concurrent embedding/retrieval work for identical authorized cache keys. Use bounded waits and owner-token fencing/expiry. A crashed leader must not strand requests. Followers can retry once or execute through the normal path after the wait budget; document that this bounds overhead rather than guaranteeing exactly-once paid execution.
- Never coalesce chat commits or ownership checks. A cached result must be deep-copied/immutable before downstream ranking or formatting mutates metadata.
- Attribute real provider calls to the leader/request that made them. Followers log cache/reuse origin, their own latency and zero new provider work; do not count historical cached usage as freshly billed spend. Keep logical requests and actual upstream calls separate.

**Acceptance:** Cold/warm equivalence; revision/namespace/date/model/policy changes miss; permissions checked on every request; two users/scopes/Lab revisions cannot leak data; publish/read races, deletion, malformed cache data, Redis outage, eviction, leader crash and lock expiry recover; no mutable cache contamination; actual provider work metered exactly once in normal coalescing tests. Run isolated PostgreSQL and Redis integration tests before enabling distributed reuse.

## G. Equivalent database/network work with lower latency

**Files:** `retrieval.py`, `legal_retrieval.py`, `repositories.py`, `rag_lab_repository.py`, `services/gemini.py`, `services/jev.py`, `services/storage.py`, `app.py`, `token_usage.py`; additive SQL RPC migration(s).

1. B's bounded recent-message query is the first performance fix. Public history retrieval remains compatible.
2. Add an additive batch-neighbor RPC accepting bounded anchors with document/revision identity. Return anchor mapping explicitly and preserve same-document/radius rules and per-anchor revision filtering. Implement a Lab adapter with file/revision semantics. Keep the single-anchor method as a tested compatibility fallback for unmigrated deployments.
3. Execute independent hybrid searches concurrently after their embeddings exist. In legal mode, finish handbook retrieval before deriving subsequent query identifiers; then bound independent stage searches. Relationship search waits for its seeds. Restore original stage/query ordering before merge/rank so network completion order cannot alter ties or citations.
4. Start with opt-in maximum two concurrent searches/request and a process-wide bound; retain existing distributed request admission. Default concurrency remains one until load tests establish safe capacity. No unbounded per-request thread pools and no multiplication of provider retry budgets.
5. Handle a failed required branch with the existing error/fallback semantics. Do not quietly generate an answer from partially failed mandatory retrieval. If the legacy behavior retried, keep retries bounded and accounted for.
6. Use managed HTTP sessions/connection pools with explicit concurrency ownership and lifecycle cleanup. Do not assume a mutable shared Requests or Supabase client is safe across new worker threads. Choose thread-owned clients or a documented safe transport; never mutate a shared query builder.
7. Propagate request context into workers correctly. Current usage accounting stores mutable request-local lists in a `ContextVar`; naive executor use loses correlation or introduces shared mutation races. Prefer returning worker results/usage to the parent for aggregation, or implement explicit synchronized collection with tested context propagation. Account for work that completes after cancellation before closing the query trace.

**Acceptance:** Sequential and concurrent modes produce identical ordered evidence/citation mappings on deterministic fixtures, including ties. Tests demonstrate bounds, no request-context leakage, correct counts under retries/failures, revision-safe neighbors, cleanup on shutdown, and unchanged access checks. Load tests report p95, errors and retries, not only an isolated speedup. Network batching/concurrency does not by itself justify a token-cost saving claim.

## Cross-cutting migration and configuration contract

- Check migration numbering at implementation time. At this baseline the next available number is 014. Prefer separate additive migrations for query helpers and cache generations. Do not overwrite existing migrations or drop old RPC signatures.
- Test fresh install, upgrade, repeated application, publication/deletion races and backend-role permissions on disposable PostgreSQL with pgvector. New tables/RPCs must retain existing service-role boundaries; apply RLS/grants deliberately. Rollback is primarily disabling new callers/features; do not drop data while an old/new deployment may still use it.
- Keep feature settings centralized in `settings.py`/`jev_settings.py` with validation and safe status output. Update Render/environment templates consistently. Do not edit local `.env` secrets or make experimental flags default-on accidentally.
- Suggested independently testable settings: `RAG_QUERY_CONTEXT_V2`, `RAG_CONTEXT_RESOLVER=legacy|combined`, `RAG_PRUNE_INEFFECTIVE_DECISIONS`, `JEV_SHADOW_SAMPLE_RATE`, `RAG_PLANNING_POLICY=legacy|v2`, `RAG_ROUTE_POLICY=legacy|v2`, `RAG_EVIDENCE_POLICY=legacy|v2`, `RAG_ADAPTIVE_RETRIEVAL`, `RAG_VERIFICATION_POLICY=legacy|v2`, `RAG_EMBEDDING_CACHE`, `RAG_RETRIEVAL_CACHE`, `RAG_ANSWER_CACHE`, `RAG_RETRIEVAL_CONCURRENCY`, `RAG_BATCH_NEIGHBORS`, plus bounded TTL/lock timeout settings.
- These names are a proposed implementation contract. Consolidate only when rollback and attribution remain independently possible, and document the final names. Include the effective configuration digest in traces and cache keys. Existing Jev off/shadow/active and technical-failure settings remain authoritative within legacy mode.
- Default the new behavioral policies and caches off, resolver to legacy, shadow sample rate to 1, and concurrency to 1. Enable candidates explicitly in tests/evaluation. Promote proven behavior-preserving fixes separately after regression verification. Provide one rollback manifest restoring legacy behavior and bypassing all new caches.
- The existing `docs/` directory is ignored by Git. Put durable implementation/runbook documentation in a tracked location (for example `engineering/answer-pipeline/`) or explicitly address repository convention. Keep private corpus-derived reports in ignored artifacts. Do not accidentally omit the handoff/runbook from the deliverable because of ignore rules.

## H. Verification, rollout gates, and definition of done

### Local checks

Use the project virtual environment. The audit downloaded the public tokenizer data into `/private/tmp/answer-audit-tiktoken`; if still present, set `TIKTOKEN_CACHE_DIR` to that path. If absent, obtain the ordinary public dependency rather than mocking tokenization to make tests pass.

```sh
TIKTOKEN_CACHE_DIR=/private/tmp/answer-audit-tiktoken .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check <changed-python-files>
RAG_POSTGRES_TESTS=1 TIKTOKEN_CACHE_DIR=/private/tmp/answer-audit-tiktoken .venv/bin/python -m pytest -q tests/test_atomic_indexing_postgres.py
RAG_REDIS_TESTS=1 TIKTOKEN_CACHE_DIR=/private/tmp/answer-audit-tiktoken .venv/bin/python -m pytest -q tests/test_security_redis.py
```

Extend those isolated integration suites or add dedicated suites for new SQL/cache behavior. The PostgreSQL suite uses a disposable network-disabled pgvector container; the Redis suite uses an isolated local socket. Never repoint them at production credentials. Report unavailable dependencies/skips explicitly. Run focused tests after each package and the complete suite at integration; do not repeatedly run unrelated tests without a new change or concern.

### Release gates

| Gate | Required evidence |
|---|---|
| Behavior-preserving pruning/transport | Regression suite passes; deterministic outputs and ordering equivalent; intended calls/reads demonstrably removed; no failure-policy regression |
| Context/evidence/route/planning/verification changes | Held-out review shows no new critical unsupported legal claim, material exception loss, wrong applicability or cross-user leakage; supported-answer coverage and false abstention do not worsen within each evaluated slice; report sample size and uncertainty |
| Retrieval change | Required-evidence recall preserved/improved; broader searches and exception/update checks remain where needed; fewer irrelevant tokens or better quality demonstrated |
| Cost optimization claim | Complete priced coverage or explicitly limited token/call comparison; include failures, all decision/audit work and cold starts; cheaper generation alone is insufficient |
| Performance claim | End-to-end p50/p95 and error/retry rates measured under comparable concurrency; no p95 regression hidden by a faster average |
| Cache enablement | Isolated invalidation, permission, race, outage and coalescing tests pass; warm results correct; cold overhead recorded; measured hit rate supports expected benefit |
| Default/production promotion | Deployed config known, target migration validated, operational authorization exists, feature-specific rollback ready and representative quality evidence available |

Use zero new critical errors as a release blocker on the evaluated cases, not a claim of zero real-world risk. For each cost/latency feature, require a repeatable benefit exceeding observed run-to-run noise. A quality fix may warrant higher cost, but identify that trade-off explicitly rather than counting it as savings. Do not offset new unsupported claims against savings elsewhere.

### Rollout sequence

1. Ship/prepare telemetry and context/DB compatibility plumbing with legacy behavior selectable.
2. Enable proven no-effect decision pruning and lean history reads.
3. Pilot context, evidence and verification policies independently on fixed revisions and reviewed questions. Compare the final combined policy for interaction effects.
4. Apply validated additive migrations in the authorized environment; enable embedding cache, then retrieval cache; enable narrowly eligible answer caching last.
5. Enable batched neighbors and bounded search concurrency after equivalent-output and load checks. Observe retry and connection pressure.
6. Use a controlled rollout cohort with held-out review. Broaden only when quality, coverage, latency and accounting remain acceptable. Disable the specific feature on regression; corpus changes remain atomically published and independently reversible through the existing revision workflow.

### Required completion package

- Implementation and meaningful tests for B–G; telemetry/evaluation tooling from A.
- Migration files, compatibility tests and a reviewed migration inventory.
- A concise change log mapping every delivered change to priorities 1–6 and its feature flag.
- Baseline/candidate evaluation tables by question class, including failures, cost completeness, cold/warm/cache behavior and concurrency.
- Updated safe configuration examples, feature dependencies and exact enable/rollback instructions.
- Explicit status for each feature: implemented, locally tested, benchmarked, rollout eligible, deployed. Do not collapse these into a single “done.”
- Remaining operational inputs, if any: deployed settings, representative approved questions/labels, live evaluation budget, target environment/migration approval. Complete all independent code/tests before presenting these as blockers to rollout.

## Start instruction for the implementation agent

Read this handoff and the current-system audit; inspect repository drift and instructions; execute A, then B, C, D, E, F, G and H with the dependencies above. Keep progress in a checklist with evidence attached to each package. Implement all six priorities in the local code and tests; keep behavior-changing defaults behind the specified gates. Produce a concrete, reviewable implementation and rollout package. Do not declare production cost/accuracy improvements without the corresponding measurements.
