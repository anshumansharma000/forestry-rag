# Handbook-led legal retrieval

This is an opt-in backend workflow over existing ingested documents. It does not rebuild embeddings,
replace chunks, or change existing API request/response models. It adds a separate reviewed legal registry
and three kinds of admin operations: inspect/suggest annotations, save annotations, and preview retrieval.

## Compatibility and shared database deployment

- Apply `migrations/010_legal_registry.sql` as a separate deployment step. It creates one new table and a
  new RPC, `match_legal_chunks_v1`. It does not alter existing tables or the `match_document_chunks` RPC.
- The old application continues using its original schema and RPC. The new application with
  `RAG_LEGAL_HIERARCHY=false` also works without migration 010.
- Existing documents need no backfill or re-ingestion. Missing legal annotations remain unknown;
  every retrieval stage also searches legacy chunks. Existing ingestion cannot overwrite the sidecar.
- All new registry reads/writes use the server's explicit `LEGAL_REGISTRY_NAMESPACE`. Configure `dev`
  on development and `prod` on production. There is no default namespace and no client override.
- Registry writes require an authenticated admin and `LEGAL_REGISTRY_WRITES_ENABLED=true`.
  The table has RLS enabled with no public policies; use the server service-role credential.
- `/admin/legal/preview` reads the corpus and returns results without persisting chats, trials, or annotations.
  It calls the embedding service; optional answer generation also calls the configured generation service.
- Shared database credentials are not isolation by themselves. **Do not use ordinary upload, replace,
  ingest, or RAG Lab publish endpoints for production-corpus experiments.** These existing endpoints still
  write to shared data. Use offline fixtures or unpublished RAG Lab revisions for ingestion tests.
  Legal preview searches the regular corpus, not unpublished lab revisions.

Development configuration:

```env
RAG_LEGAL_HIERARCHY=false
LEGAL_REGISTRY_NAMESPACE=dev
LEGAL_REGISTRY_WRITES_ENABLED=true
```

This permits new admin previews and dev-scoped annotations while ordinary `/ask` and chat remain unchanged.
After validation, setting `RAG_LEGAL_HIERARCHY=true` on **dev only** enables the pipeline for `/ask` and chat.
Production should retain `false` and write protection until a reviewed rollout. Namespace annotations do not
promote automatically. Review and save them separately through the production deployment when authorized.
Rollback is setting the flag to `false`; leave the additive migration in place for older/newer binaries.

## Review existing documents without re-ingesting

Use existing `GET /documents` pagination to obtain document UUIDs. As an admin:

1. `GET /admin/legal/profiles/{document_id}/suggestion` returns an unreviewed classification inferred from
   the title, plus existing explicit date metadata. It writes nothing and does not infer legal relationships.
2. Verify the original document, then `PUT /admin/legal/profiles/{document_id}` with a complete profile.
   PUT replaces only that namespace's profile, so include relationships you want to retain.
3. `GET /admin/legal/profiles` returns that namespace's saved profiles (maximum 10,000; errors rather than
   silently truncating an oversized registry).

Example reviewed profile (dates below are illustrative, not legal findings):

```json
{
  "instrument_type": "amendment",
  "issued_date": "2024-09-20",
  "effective_date": null,
  "jurisdiction": "India",
  "enabling_provision": "Verify and enter the enabling provision",
  "reviewed": true,
  "relationships": [
    {
      "target_document_id": "00000000-0000-0000-0000-000000000001",
      "relation": "amends",
      "source_provision": "Amending provision and page",
      "target_provision": "Affected rule/sub-rule",
      "effective_date": null,
      "evidence": "Exact passage establishing the relationship"
    }
  ]
}
```

Allowed instrument types: `unknown`, `handbook`, `act`, `rules`, `amendment`, `guideline`, `notification`,
`clarification`, `instruction`, `judicial`. Links also support `supersedes`, `repeals`, `interprets`, `stays`,
`clarifies`, and `refers_to`. Judicial status is separate from instrument type. Unreviewed profiles never
supply authoritative date overrides, targeted stage matches, or relationship traversal.

## Read-only dev preview

`POST /admin/legal/preview`:

```json
{
  "question": "Which provisions govern diversion of forest land?",
  "as_of": "2025-01-01",
  "jurisdiction": "Goa",
  "generate_answer": false
}
```

The response retains the usual answer/source fields and adds `review` and `evidence` on this **new** endpoint.
`generate_answer=false` skips answer generation; it still embeds queries. Set it to true for end-to-end QA.
An optional `top_k` retains the existing 1–20 range; small budgets may omit required categories, which the
review reports. The normal legal-mode default accommodates ten excerpts and an 8,000-token selection budget.

The pipeline searches handbook, Act, rules, amendments, implementation material and judicial material.
Handbook references inform later queries, but never become verified relationships automatically. Both incoming
and outgoing reviewed links are followed, bounded to three hops and 30 related documents. Truncation is exposed.
Each stage searches up to 30 classified and 30 legacy candidates; linked documents supply up to three chunks.
The six stages each make one embedding call and two search calls; relationship retrieval adds bounded work.
This has a higher latency/cost than the default path and should be measured in dev before enabling broadly.

Category coverage means a relevant-looking reviewed excerpt survived selection, **not** a legal determination
that the stage is complete. The answer model receives the requested date/jurisdiction, annotations, source
passages, missing evidence and explicit precedence instructions. It must resolve applicability from excerpts,
not dates alone. The system does not automatically consolidate statutory text or prove court-order currency.
The collection is explicitly marked `unverified` for currentness until a separate corpus audit is completed.

## Validation and rollout gates

Run `.venv/bin/python -m pytest -q` and `.venv/bin/python -m ruff check .` locally. Tests mock external services
and never use production data. `tests/test_legal_hierarchy.py` covers default-off compatibility, legacy rows,
API shapes/access, namespace scoping, required stages, incoming amendments, cycles, historical scope, missing
evidence and read-only previews.

Before deploying: exercise the SQL migration twice on a **disposable Supabase/Postgres database with pgvector**,
confirm the old RPC still returns its original shape, verify anon/authenticated cannot access the new registry,
and confirm dev/prod sidecar rows cannot mix through the application. SQL has not been executed by local unit tests.

Then have the SME label 15–20 real questions covering outdated handbooks, partial amendments, historical dates,
interim/follow-up orders, contradictory sources, missing documents and unrelated matches. Compare ordinary and
legal-preview answers, exact citations, omissions, latency and cost. Do not equate passing mocked tests with
validated legal-answer quality. No automatic database migration, annotation backfill, or production enablement
is performed by this change.

## Existing-corpus backfill

`scripts/legal_backfill.py` prepares draft annotations from a read-only JSON snapshot containing `documents.json`
and `chunks.json`. It checks source text and self-identification clauses instead of treating every cited Act as
the document's own type. References with multiple possible target editions are queued, not arbitrarily linked.
All generated profiles retain `reviewed=false`; neither their labels nor candidate relationships are activated
as reviewed authority until a reviewer approves them through the admin API.

```bash
PYTHONPATH=. .venv/bin/python scripts/legal_backfill.py prepare \
  --snapshot /path/to/read-only-snapshot --manifest data/legal_registry_backfill/dev_annotations.json
LEGAL_REGISTRY_NAMESPACE=dev PYTHONPATH=. .venv/bin/python scripts/legal_backfill.py apply \
  --manifest data/legal_registry_backfill/dev_annotations.json
```

Apply is insert-only with conflict-ignore semantics and refuses production namespaces. Existing profiles are
preserved, including during concurrent reviews. Run preparation again when the corpus changes; this tool does
not automatically refresh existing annotations. Database-derived artifacts under `data/legal_registry_backfill/`
are excluded from Git.

On 19 September 2026, after the user applied migration 010, the dev registry was populated with 358 draft profiles
and 110 candidate relationships. Both the existing and new search RPCs were exercised successfully. An idempotent
backfill rerun and before/after fingerprints verified preservation of shared document and chunk text/metadata.
The local verification report and review queues are in `data/legal_registry_backfill/`. This operational check is
separate from the mocked unit tests; it does not establish legal correctness or currentness.
