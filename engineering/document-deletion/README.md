# Admin document deletion

Apply `migrations/017_admin_document_deletion.sql` after migrations 011, 013 and
014, then deploy the backend and frontend together. This adds service-role-only
RPCs; no reindex is needed. The migration has not been applied to a live database.

`DELETE /documents/{document_id}` requires a bearer token and a current database
user whose exact role is `admin`, including when `AUTH_DISABLED=true`.

- 200: `{"status":"deleted","document_id":"<uuid>"}`.
- 401/403: missing/invalid authentication or insufficient permission.
- 404: no document with this ID (also returned on a new request after deletion).
- 409: ingestion/upload holds the library lease, or this source/library has queued
  or running ingestion. Retry after processing finishes.
- 503: storage or final database cleanup failed. The response explains partial
  database cleanup and instructs the admin to retry Delete.

The original is removed from local storage or R2 first. Missing originals are
accepted so cleanup can be retried. Only after storage succeeds does a database
transaction delete the document, its live chunks, index revisions, revision chunks
and optional legal profiles, invalidate caches via migration 014, and insert the
`documents.delete` audit event with actor and filename. Existing chat history and
independent Lab experiments are retained. A deleted citation download returns 404.

Deletion, ingestion and every final upload write share `documents:ingest`.
Uploads also check the database when the original is missing, preventing a live
index from being overwritten during cleanup retries. Already queued jobs block
preflight; a job newly queued during deletion cannot acquire the ingestion lease
until deletion finishes, at which point its original is absent.

Storage and Postgres cannot commit together: if storage succeeds but Postgres
fails, the document row/index remains until the admin retries. Its original download
is unavailable in that interval, and existing indexed evidence may still be used.
The UI retains the dialog and error, and repeated storage deletion is harmless.
If a success response is lost, reloading the library reconciles the deleted row.
An answer already in progress may still finish with evidence it previously read.

The library shows Delete only to admins on desktop and mobile, asks for explicit
confirmation with the filename, prevents repeated clicks while pending, displays
backend errors, then reloads results and corrects pagination after success.

Offline verification: `pytest -q tests/test_document_deletion.py tests/test_upload_recovery.py`.
Real database verification uses a disposable Docker instance, never configured
Supabase: `RAG_POSTGRES_TESTS=1 pytest -q tests/test_document_deletion_postgres.py`.

## Browser reports CORS plus HTTP 500

An unexpected backend error used to lose CORS headers because the framework's
outer error handler sat outside the CORS layer. CORS now wraps the complete error
stack; allowed browser origins can read the existing structured error response,
and untrusted origins remain blocked.

If deletion returns `config_error` asking for migration 017, run
`migrations/017_admin_document_deletion.sql` in the connected Supabase project's
SQL editor. The script is repeatable and now requests a PostgREST schema-cache
reload after creating both deletion functions. If the migration was already run
but the API still cannot find the functions, run `NOTIFY pgrst, 'reload schema';`
in that same project's SQL editor. Restart the backend if it is not auto-reloading.
Preflight failure occurs before file deletion; retry after the database setup is complete.
