# P0 fixes: source preservation, validation, and atomic indexing

Normal ingestion builds `document_index_revisions` and `document_revision_chunks` without modifying the active document or its searchable chunks. Chunk inserts are allowed only while a revision is building; stored chunk content and document snapshots are immutable. Empty, incomplete, failed, or stale revisions cannot publish.

`publish_document_revision` locks the document and revision, validates the complete chunk sequence, replaces the active chunks, and updates document metadata in one PostgreSQL transaction. Readers see the old committed index until the replacement commits. Publication or another document-row update invalidates other builds from the prior generation; stale builds must rebuild. Among competing publications from the same generation, the first successful publication wins. Existing RAG Lab publication updates the generation too. Retrying an already committed publication is a no-op, including if its response was lost.

Failures preserve any previously indexed document. They are visible on the job/revision; refresh errors are additionally stored as `last_refresh_error` and `last_refresh_failed_at` on the document. A brand-new failed document remains `failed`. An interrupted staging build never enters retrieval. Prior revision chunks remain stored for inspection; this change does not add a rollback endpoint or automatic retention policy. Explicit deletion of a revision can cascade its archived chunks after a suitable retention policy is established.

Hybrid search, optional legal search, and neighbor lookup require `documents.metadata.ingest_status = indexed`. New chunks carry `index_revision_id`, and neighbor expansion checks that identifier to avoid mixing a new index's neighbors with an earlier anchor. Existing indexed chunks remain searchable. Legacy chunks with missing/failed/indexing document status are excluded rather than assumed valid.

Heading cleanup now changes heading metadata only: original heading text, including dash-separated fees, dates, and exceptions, stays in chunk content. FAQ heading text is retained too. Validation responses expose only error type/location/message and return 422 for model-validator errors without serializing exception objects or echoing submitted input.

## Deployment

1. Drain and stop old ingestion workers; do not run the old delete-first ingestion implementation alongside the new one.
2. Apply `migrations/011_atomic_document_indexing.sql` to the existing database before deploying this code. Fresh installations first apply `supabase_schema.sql` and `migrations/009_rag_lab.sql`, then 011. Optional migration 010 may be installed before 011; the checked-in 010 also includes the indexed-only filter for later installation.
3. Deploy the API and workers together. Application startup does not run migrations.
4. Check an explicit source refresh and its job result. While it runs, the previous indexed document should remain available.
5. Re-ingest existing sources explicitly (or deliberately increment `RAG_INDEX_VERSION`) to repair previously lost heading content. The Python fix cannot restore text already removed from stored chunks.

The migration does not re-embed or delete existing active chunks. It creates backend-only staging objects with restricted access; the separate permissions/RLS audit of existing Supabase objects remains out of scope. No live database migration is performed by the test commands below.

Do not roll workers back to the delete-first code. Original file versioning, full-request snapshot isolation across multiple retrieval RPCs, automatic cleanup of abandoned revisions, and total job-level deduplication are separate follow-up work. Publication is atomic per document, not for an entire multi-document ingestion job.

## Tests

```sh
.venv/bin/python -m pytest -q
```

The real PostgreSQL suite requires Docker and `pgvector/pgvector:pg16` (pull the image once if needed):

```sh
RAG_POSTGRES_TESTS=1 .venv/bin/python -m pytest -q tests/test_atomic_indexing_postgres.py
```

It creates and removes its own network-disabled container. It never uses application credentials or a Supabase URL. Assertions cover incomplete/failed builds, immutable revisions, stale publication, idempotent publication retries, old-index visibility during a concurrent transaction, indexed-only hybrid/legal/neighbor retrieval, Lab publication compatibility, and backend-role execution.
