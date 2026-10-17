# Answer pipeline: priorities 1–6

Follow-up: [Query cost policies](../query-cost/README.md) adds opt-in Jev-led routing,
independent audit selection, exact evidence compaction and complete query API accounting.
Its candidate/rollback manifests and offline report are separate from this rollout.

Follow-up: [Lite-first routing](lite-first-routing.md) changes the default model-routing policy at the user's request. Routine bounded answers now prefer Lite; the original priorities 1–6 feature gates below otherwise retain their defaults.

The implementation introduces consistent request interpretation, evidence diagnostics/selection, optional decision pruning, explicit verification, exact versioned caches, and bounded retrieval concurrency. Public API response shapes, outcome strings, `rag.py` imports and atomic turn/index operations remain compatible.

This is a local implementation. Behavioral policies and cache/transport features default off. Deployment templates explicitly retain those defaults. The existing local `.env` was not edited. Lean latest-ten history fetching and the Lab/main-corpus isolation fix are immediate correctness/performance changes.

Read [implementation-plan.md](implementation-plan.md) for the agreed scope, [progress.md](progress.md) for completed work and [validation-summary.md](validation-summary.md) for the release status and check results. Synthetic reports are fixtures and diagnostics, not evidence of production savings or real-model legal accuracy.

## Delivered changes

| Priority | Implementation | Primary controls |
|---|---|---|
| 1 | Immutable question/history/constraint context, exact-span validation and numeric-scope checks; context reaches retrieval interpretation, planning, draft and verifier; selectable combined resolver | `RAG_QUERY_CONTEXT_V2`, `RAG_CONTEXT_RESOLVER=legacy|combined` |
| 2 | Explicit corpus/Lab/namespace identities; relevant qualification protection; single bounded adaptive expansion; coverage diagnostics; actual numbered heading ancestry; Unicode lexical terms preserving Hindi marks/negation; merged-caption table headers and multilingual-safe corruption diagnostics | `RAG_EVIDENCE_POLICY=legacy|v2`, `RAG_ADAPTIVE_RETRIEVAL`, `RAG_EXTRACTION_DIAGNOSTICS` |
| 3 | Effective-question truth table for shared routing/planning; shadow sampling; metadata allowlist; independently gated small-synthesis planning and reviewed-principal-instrument routing shortcut; optional follow-up | `RAG_PRUNE_INEFFECTIVE_DECISIONS`, `JEV_SHADOW_SAMPLE_RATE`, `RAG_PLANNING_POLICY`, `RAG_ROUTE_POLICY`, `RAG_OPTIONAL_FOLLOWUP` |
| 4 | Lightweight audits on previously unaudited factual answers in v2; structured issue codes, explicit owner/reason, exact-quote checks, existing bounded Gemini repair/escalation, unchanged final negative Jev semantics | `RAG_VERIFICATION_POLICY=legacy|v2` |
| 5 | Query and batch-query embedding caches; retrieval cache; narrow audited standalone answer cache; bounded coalescing; transactional corpus/registry versions; access/model/date/policy isolation; immutable copies and fenced writers | `RAG_EMBEDDING_CACHE`, `RAG_RETRIEVAL_CACHE`, `RAG_ANSWER_CACHE`, cache settings below |
| 6 | Latest-ten minimal-field history reads, additive batch neighbors for corpus and Lab, at most two searches/request and four active/process, ordered merges, thread-owned transports, usage-context propagation and shutdown cleanup | `RAG_BATCH_NEIGHBORS`, `RAG_RETRIEVAL_CONCURRENCY=1|2`, `RAG_HTTP_POOLING` |

The resolver retains all recent user messages and the latest assistant reply; ambiguity retains the complete recent window. A standalone-search decision does not authorize dropping user constraints. Conversation text establishes intent/previous wording, not legal authority. Constraints outside the ten-message window are not newly recovered.

V2 evidence coverage remains `unverified`: diagnostics are not a calibrated probability of answer correctness. The existing numeric retrieval-confidence gate is unchanged. New qualification promotion preserves the first anchor and all candidate passages; it does not delete merely similar legal text. Adaptive retrieval is one expansion for narrow questions with no lexically related candidates; broad questions retain batched facets and identifier/update checks.

V2 verification can increase cost for previously unaudited answers. It uses the existing bounded audit repair/escalation path; no unbounded repair or retrieval loop was introduced. A negative active Jev verdict is final. Changing verifier ownership requires a separate evaluated policy decision.

## Rollout order

1. Record the target deployment's effective settings and baseline traces; local active Jev settings differ from shadow deployment templates.
2. Review the lean-history and Lab scope fixes with existing client contracts. Adopt decision pruning independently after comparing its removed calls.
3. Enable context/combined resolution, evidence and verification candidates one at a time against fixed approved evidence and held-out expert labels. Review the combined policy separately.
4. Apply additive migrations 014 and 015 to the authorized target environment after disposable-database validation. Application startup never applies migrations.
5. Enable embedding cache, then retrieval cache. Keep answer caching restricted and off until validated. Turn on batched neighbors and bounded concurrency after an equivalent-ordering/load comparison.
6. Compare coverage, unsupported claims, missing conditions, false abstention, applicability, total priced cost, unpriced usage and p50/p95 latency per question class. Pause promotion on a critical quality regression.

`candidate.env` is an evaluation manifest. `rollback.env` restores the legacy policies and bypasses new caches. It intentionally does not change existing Jev modes, provider models, security settings or the old cost controls. Feature dependencies are independent: v2 context is recommended for other v2 policies, but legacy adapter calls remain supported. Combined resolution is selected only with context v2 enabled.

## Migrations and compatibility

- `014_answer_cache_versions.sql`: transactional generation counters/triggers for live documents/chunks, legal namespaces and Lab lifecycle. Staged chunks do not invalidate the live corpus. Direct live edits/deletes, normal/Lab publication and registry namespace moves invalidate the relevant cache identity.
- `015_answer_query_helpers.sql`: latest-history index, additive bounded batch-neighbor RPCs. Old RPC signatures remain available. Only missing-function errors permit single-anchor fallback; storage failures propagate.
- Apply after existing migrations. If the optional legal registry or Lab schema is installed later, rerun 014/015 to attach their optional triggers/helpers before enabling those caches. DDL changes and provider alias changes require a new `RAG_CACHE_EPOCH`/cache namespace. These are not inferred from row counts or timestamps.
- A missing version RPC or unready/deleted Lab revision disables reuse. Cache consistency is bound to a checked version-read point; a publication after that point can race with a request just as in the uncached path. A generation change during work prevents cache publication.
- Rollback disables new callers; leave additive migrations in place while old and new binaries may coexist.

## Cache operation

Set a dedicated `RAG_CACHE_REDIS_URL` and a secret `RAG_CACHE_HMAC_KEY` of at least 32 bytes through the deployment secret manager. The cache uses its own connection pool and key namespace; rate-limit security retains its separate fail-closed behavior. An optional cache outage bypasses reuse without repeating a failed model computation. Configure Redis TLS/access controls and server memory appropriate to the deployment.

Default limits:

| Setting | Default / bound |
|---|---|
| Embedding TTL | 24 hours; whole exact query batches reuse as a batch |
| `RAG_RETRIEVAL_CACHE_TTL_SECONDS` | 300; 1–300 |
| `RAG_ANSWER_CACHE_TTL_SECONDS` | 300; 1–300 |
| `RAG_CACHE_WAIT_MS` | 100; 0–3000 |
| `RAG_CACHE_LOCK_SECONDS` | 60; 1–300 |
| `RAG_CACHE_MAX_ENTRIES` | 512; 1–10000 |
| `RAG_CACHE_MAX_VALUE_BYTES` | 524288; 1024–4194304 |
| `RAG_CACHE_EPOCH` | 1; change on unversioned model/prompt/schema releases |

The keyspace is bounded by entries and per-value size. Publication evicts the earliest-expiring entry at capacity; Redis can also evict independently. Followers wait a bounded interval, then may compute without caching. Lock expiry permits another owner; owner-token fencing prevents a late writer overwriting a newer result. This bounds duplicate work rather than promising exactly-once paid execution.

Embedding keys bind exact prepared input/batch, provider account, model, dimensions and preprocessing. Retrieval keys also bind access, corpus/registry generation, Lab revision, date, resolved constraints and effective policy. Answer keys additionally bind exact original request and output policy; values require a recorded successful support audit. Answer caching initially applies only to guarded standalone `/ask` fact lookups. It excludes chat envelopes, current/historical/applicability requests, errors, abstentions and unverified drafts. Cached values are schema/citation checked and deep-copied; cache reads do not skip endpoint authentication.

Do not enable response reuse solely because a string looks similar. There is no semantic answer cache. Cache hits log no fresh upstream charges; generation/embedding usage is counted on the request that actually made the call. Cold-path version checks and lock/Redis overhead must be included in evaluation.

## Reproducible validation

```sh
TIKTOKEN_CACHE_DIR=/private/tmp/answer-audit-tiktoken .venv/bin/python -m pytest -q
RAG_POSTGRES_TESTS=1 RAG_REDIS_TESTS=1 TIKTOKEN_CACHE_DIR=/private/tmp/answer-audit-tiktoken .venv/bin/python -m pytest -q
TIKTOKEN_CACHE_DIR=/private/tmp/answer-audit-tiktoken .venv/bin/python -m scripts.evaluate_pipeline --output engineering/answer-pipeline/replay-results.json
TIKTOKEN_CACHE_DIR=/private/tmp/answer-audit-tiktoken .venv/bin/python -m scripts.evaluate_extraction --output engineering/answer-pipeline/extraction-pilot.json
```

PostgreSQL tests use a disposable network-disabled pgvector container; Redis uses isolated local sockets. Neither uses the configured live services. If the temporary tokenizer cache is absent, download the ordinary public tokenizer dependency or use an existing valid cache; do not mock tokenization.

The evaluator uses local supplied evidence, a deterministic lexical repository adapter, and real orchestration. Replay uses scripted model outputs and is suitable for call-path regressions only. The live option makes paid model calls on that local evidence and remains opt-in; it does not benchmark SQL retrieval or provide expert labels. It requires positive `--max-requests`, `--max-tokens`, `--max-usd` and writes partial results. Token/price reservations are conservative estimates using the repository rate card; they are not provider-enforced billing limits. Unknown usage/pricing or transport failure stops subsequent live calls. The hard request ceiling also applies to parse/truncation retries through the bounded client. Add verified embedding pricing support before attempting a complete live retrieval/model cost comparison; the harness stops before an embedding request with unknown pricing, or before further requests if reported usage is incomplete.

The initial production-readiness target remains 120 representative expert-labeled cases, with a frozen held-out split and repeated live runs within a separately agreed budget. Include exact earlier quotes, exceptions, corrections and multilingual cases. No such approval/labels are manufactured by the synthetic suite.

## Remaining deployment inputs

The local implementation does not establish the deployed policy, billing rates, corpus currentness or real-model quality. Promotion requires the target environment, reviewed migration execution, approved representative evidence/labels and a bounded live-evaluation budget. There has been no live corpus reindex, annotation promotion, deployment or paid model evaluation during implementation.
