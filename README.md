# Forest Department Pilot RAG

Optional Jev integration evaluates verification, evidence ranking, chat rewriting,
model/reasoning routes, planning, history selection, document classification and
extraction quality. Each mode controls task ownership (`off`, `shadow`, or exclusive `active`). See [Jev rollout and evaluation](docs/jev.md)
for key configuration, independent rollout modes, fallbacks, and cost accounting.

This is a pilot-ready RAG API foundation for forest department source documents. It stores source files locally for now, extracts text from `.pdf`, `.docx`, `.txt`, `.ppt`, and `.pptx` files, chunks the text, creates embeddings with the Gemini API, stores chunks in Supabase Postgres + pgvector, and answers questions with citations.

Phase 1 hardening adds bearer-token auth, roles, user-scoped chat sessions, upload restrictions, audit events, structured errors, runtime config validation, migrations, and Docker deployment files. The current codebase also separates routes, repositories, document loading, chunking, retrieval, prompt construction, chat, and ingestion into focused modules.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Add your Gemini API key to `.env`.

Create a Supabase project, open the SQL editor, and run:

```text
supabase_schema.sql
```

For an existing PoC database, run these migrations instead of recreating everything:

```text
migrations/001_phase1_pilot_hardening.sql
migrations/002_jwt_auth.sql
migrations/003_password_login_refresh_tokens.sql
migrations/004_ingest_jobs.sql
migrations/005_hybrid_retrieval.sql
migrations/006_fix_match_document_chunks_ambiguous_id.sql
migrations/007_document_library.sql
migrations/008_failed_documents_library.sql
migrations/009_rag_lab.sql
migrations/011_atomic_document_indexing.sql
```

New installations should run `migrations/009_rag_lab.sql` and then `migrations/011_atomic_document_indexing.sql` after `supabase_schema.sql`. Migration 011 is required by ingestion and neighbor retrieval. The optional legal registry uses migration 010; migration 011 also updates its retrieval function when installed. See [atomic indexing rollout](docs/atomic_indexing.md) before deploying.

If you already created the wrong vector dimension while experimenting, run:

```text
supabase_reset_768.sql
```

That drops and recreates the toy tables with `embedding vector(768)`. It deletes indexed documents, which is fine for this proof of concept.

Then add these values to `.env`:

```text
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
EMBEDDING_DIMENSIONS=768
BOOTSTRAP_ADMIN_TOKEN=<long random token>
JWT_SECRET_KEY=<long random jwt secret>
JWT_EXPIRES_MINUTES=1440
REFRESH_TOKEN_EXPIRES_DAYS=30
```

`SUPABASE_URL` must be the project API URL:

```text
https://<project-ref>.supabase.co
```

Do not use the Postgres connection string as `SUPABASE_URL`. A Postgres connection string starts with `postgresql://...` and is only for direct database tools.

Use the service role key only on the backend. Do not expose it in a frontend app.

## Auth and roles

All app endpoints except `/health`, `/config/status`, `/auth/login`, and `/auth/refresh` require a signed JWT access token:

```http
Authorization: Bearer <access_token>
```

Roles are ordered by permission:

- `viewer`: ask questions and manage own chat sessions
- `officer`: reserved for officer-specific pilot workflows
- `knowledge_manager`: view and upload documents, preview chunks, and run ingestion
- `admin`: create users, read audit events, and validate runtime config

Set `BOOTSTRAP_ADMIN_TOKEN` in `.env` to create the first real admin with an initial password:

```bash
curl -X POST http://127.0.0.1:8000/admin/users \
  -H "Authorization: Bearer $BOOTSTRAP_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.gov.in","password":"Temporary123","role":"admin","full_name":"Pilot Admin"}'
```

The admin-created user can then log in:

```bash
curl -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.gov.in","password":"Temporary123"}'
```

Login returns `access_token`, `refresh_token`, `token_type`, `expires_at`, `refresh_expires_at`, and `user`. Store the tokens securely and send the access token as a bearer token. JWTs are signed with `JWT_SECRET_KEY`; the backend still checks the `app_users` row on every request so disabled users, forced password changes, and role changes take effect.

Users created with `must_change_password=true` must call:

```bash
curl -X POST http://127.0.0.1:8000/auth/change-password \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"current_password":"Temporary123","new_password":"Permanent123"}'
```

Refresh access with refresh-token rotation:

```bash
curl -X POST http://127.0.0.1:8000/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token":"<refresh_token>"}'
```

Create a knowledge manager or viewer with the same endpoint using a real admin JWT. For local-only development, `AUTH_DISABLED=true` makes every request run as an admin. Do not use this in pilot production.

## Structured errors

Errors now use a consistent envelope:

```json
{
  "error": {
    "code": "auth_error",
    "message": "Bearer token is required",
    "details": {}
  }
}
```

Frontend clients should read `error.message`. The legacy FastAPI `detail` shape is not used by app handlers.

Errors are defined in `errors.py`; request models live in `schemas.py`; route modules live under `routers/`.
Keep new endpoints on the same envelope instead of returning ad hoc error shapes.

The Supabase schema uses `embedding vector(768)`, so `EMBEDDING_DIMENSIONS` must be `768` unless you also change and rerun the schema. Gemini embedding models can return 3072 dimensions by default for newer embedding models, so the app explicitly asks Gemini for 768-dimensional embeddings.

By default, supported source files go in:

```text
data/docs/
```

For Cloudflare R2 object storage, set:

```env
DOCUMENT_STORAGE_BACKEND=r2
R2_ACCOUNT_ID=<cloudflare-account-id>
R2_ACCESS_KEY_ID=<r2-access-key-id>
R2_SECRET_ACCESS_KEY=<r2-secret-access-key>
R2_BUCKET=fisrag-docs
R2_PREFIX=docs/
R2_UPLOAD_STAGING_PREFIX=pending-uploads/
PRESIGNED_UPLOAD_EXPIRES_SECONDS=900
```

When R2 is enabled, uploads are written to `r2://fisrag-docs/<prefix>/<filename>`, and ingestion lists/downloads supported files from that prefix before indexing them into Supabase pgvector.
Direct browser uploads use presigned R2 `PUT` URLs. Files first land under `R2_UPLOAD_STAGING_PREFIX`, then the backend finalizes them into `R2_PREFIX` after verifying the upload. Keep `R2_UPLOAD_STAGING_PREFIX` outside `R2_PREFIX`.

Supported formats:

- `.pdf`
- `.docx`
- `.txt`
- `.ppt`
- `.pptx`

PDF extraction uses selectable text where available and can send only deficient pages to Google Document AI Enterprise OCR. OCR output keeps the original PDF page number before entering the normal chunking and embedding pipeline.
Legacy `.doc` files are not supported yet; save or convert them as `.docx` first. PowerPoint slide text is extracted from both legacy binary `.ppt` and modern `.pptx` files; `.pptx` tables are preserved as structured blocks.

You can also upload files from Postman:

```http
POST http://127.0.0.1:8000/documents/upload
Content-Type: multipart/form-data

file=<your PDF, DOCX, TXT, PPT, or PPTX file>
```

For multiple files, send repeated `files` fields to `POST /documents/uploads`.
For browser-to-R2 uploads, call `POST /documents/uploads/presign`, upload each file with the returned `PUT` URL and headers, then call `POST /documents/uploads/complete`.
After completion, queue one job per returned filename with `POST /ingest` and `{"source":"<filename>"}`. The empty-body form remains available for deliberate corpus-wide maintenance but should not be used after routine uploads.

Uploads also accept optional structured document categories and metadata. Ingestion
adds editable suggestions with supporting excerpts, and existing documents can be
categorized without re-uploading. Apply migration 016 before submitting these fields;
see [upload categorization and frontend integration](engineering/document-categorization/README.md).

Duplicate uploads with a failed or missing index can be replaced after confirmation;
indexed documents and queued/running processing remain protected. See
[confirmed recovery uploads](engineering/upload-recovery/README.md) for the API contract.

Uploads are limited to 150 MiB per file and 50 files per batch. The direct-to-R2 flow applies the per-file and file-count limits without buffering the whole batch in the API process. The legacy multipart flow additionally limits the aggregate request body to 150 MiB to protect the 512 MB web container.

Large-PDF ingestion is memory bounded by closing each parsed PDF page, yielding chunks incrementally, and inserting embedding rows in small batches. These settings control the limits:

```env
PDF_EXTRACT_TABLES=false
DOCUMENT_AI_OCR_ENABLED=true
GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/document-ai-credentials.json
GOOGLE_CLOUD_PROJECT=<google-cloud-project-id>
DOCUMENT_AI_LOCATION=asia-south1
DOCUMENT_AI_PROCESSOR_ID=<enterprise-ocr-processor-id>
DOCUMENT_AI_OCR_LANGUAGE_HINTS=en,hi
DOCUMENT_AI_OCR_MIN_TEXT_CHARS=40
DOCUMENT_AI_OCR_MAX_PAGES=200
DOCUMENT_AI_OCR_TIMEOUT_SECONDS=60
DOCUMENT_AI_OCR_RETRY_ATTEMPTS=3
DOCUMENT_AI_OCR_MAX_REQUEST_BYTES=40000000
MAX_PDF_PAGES=500
MAX_EXTRACTED_CHARS=15000000
MAX_DOCUMENT_CHUNKS=3000
INGEST_BATCH_SIZE=24
```

`pdfplumber` table extraction is comparatively expensive. Keep `PDF_EXTRACT_TABLES=false` on a 512 MB worker unless table-aware retrieval is required and the worker has been load tested.

OCR is disabled unless `DOCUMENT_AI_OCR_ENABLED=true`. Native extraction runs first; pages below `DOCUMENT_AI_OCR_MIN_TEXT_CHARS` useful alphanumeric characters are copied into bounded one-page PDFs and sent to Document AI. `DOCUMENT_AI_OCR_MAX_PAGES` prevents unexpectedly large OCR jobs. For Render, upload the service-account JSON as the Secret File `document-ai-credentials.json`; Docker services receive it at `/etc/secrets/document-ai-credentials.json`. Never commit that JSON.

Ingestion runs through Celery. Set `CELERY_BROKER_URL` to the Redis Cloud connection URL in both the API and worker environments. Use the `rediss://...` URL when TLS is enabled in Redis Cloud. Celery requires `ssl_cert_reqs` for `rediss://` URLs; the app defaults it to `required` when omitted.

The API pings Celery before creating an ingest job. If the worker is stopped, failed, or connected to a different broker, `POST /ingest` returns `503` with `No ingestion worker is online` instead of creating a job that remains queued indefinitely. Inspect the same check with `GET /ingest/worker/status`.

```env
CELERY_BROKER_URL=rediss://default:<password>@<redis-cloud-host>:6379/0?ssl_cert_reqs=required
CELERY_VISIBILITY_TIMEOUT_SECONDS=3600
CELERY_TASK_MAX_RETRIES=3
CELERY_TASK_RETRY_BASE_SECONDS=60
CELERY_WORKER_CONCURRENCY=1
CELERY_REQUIRE_WORKER_ONLINE=true
CELERY_WORKER_PING_TIMEOUT_SECONDS=2
```

For the free Render deployment, the Docker start script runs the API and a single Celery consumer in the same web-service container:

```bash
sh /app/start-render.sh
```

There is no separate paid Render background-worker service. Redis Cloud remains the Celery broker, while the consumer shares the free 512 MB web container with the API. The consumer uses one thread and the bounded PDF/chunk pipeline. A healthy container emits `celery_worker_ready`, followed by `ingest_task_started` and `ingest_task_succeeded` or `ingest_task_failed` for each job.

## Run

```bash
uvicorn app:app --reload
```

## Admin RAG Lab

Admins can upload files into an isolated experiment, build immutable chunk/embedding revisions with per-revision settings, inspect chunks, save sandbox query trials, and publish an approved revision into the regular chat corpus. Draft lab chunks are stored separately and cannot appear in `/ask` or chat retrieval. Build and publish operations use the existing Celery worker.

Apply `migrations/009_rag_lab.sql`, then follow the endpoint workflow in [docs/rag_lab_api.md](docs/rag_lab_api.md). The frontend implementation handoff is in [docs/frontend_rag_lab_prompt.md](docs/frontend_rag_lab_prompt.md).

The API runs at:

```text
http://127.0.0.1:8000
```

Logs are emitted as JSON lines. Set `LOG_LEVEL` to control verbosity, for example `LOG_LEVEL=DEBUG uvicorn app:app --reload`.

## Postman / curl

Upload a PDF, DOCX, TXT, PPT, or PPTX file:

```bash
curl -X POST http://127.0.0.1:8000/documents/upload \
  -H "Authorization: Bearer <knowledge_manager_or_admin_token>" \
  -F "file=@/absolute/path/to/document.pdf"
```

Upload multiple files in one request:

```bash
curl -X POST http://127.0.0.1:8000/documents/uploads \
  -H "Authorization: Bearer <knowledge_manager_or_admin_token>" \
  -F "files=@/absolute/path/to/document.pdf" \
  -F "files=@/absolute/path/to/notes.txt"
```

Queue indexing for any documents that have not already been indexed:

```bash
curl -X POST http://127.0.0.1:8000/ingest \
  -H "Authorization: Bearer <knowledge_manager_or_admin_token>" \
  -H "Content-Type: application/json" \
  -d '{"source":"document.pdf"}'
```

The response includes a job id. Poll it until `status` is `succeeded` or `failed`:

```bash
curl http://127.0.0.1:8000/ingest/jobs/<job_id>
```

Check whether the API can see a live Celery consumer:

```bash
curl http://127.0.0.1:8000/ingest/worker/status \
  -H "Authorization: Bearer <knowledge_manager_or_admin_token>"
```

Ingest jobs are stored in Supabase `ingest_jobs`, so job status survives API restarts. Documents are marked with ingest status metadata while indexing runs.

Preview extracted chunks without calling the embedding API:

```bash
curl "http://127.0.0.1:8000/chunks/preview?source=document.pdf&limit=25&include_content=true&max_content_chars=1000"
```

Check environment configuration without exposing secrets:

```bash
curl http://127.0.0.1:8000/config/status
```

Validate environment configuration as an admin:

```bash
curl http://127.0.0.1:8000/config/validate \
  -H "Authorization: Bearer <admin_access_token>"
```

Ask a question:

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Authorization: Bearer <viewer_or_higher_token>" \
  -H "Content-Type: application/json" \
  -d '{"question":"Do village forest committees need a transit permit for bamboo?"}'
```

Every returned citation includes the stable owning `document_id`. Admins can stream its current original file with `GET /documents/{document_id}/download`; the endpoint always requires a bearer token and re-checks the user's current server-side role. Replacing a file keeps the document ID and uses current-original semantics rather than retaining downloadable historical revisions.

## Chat API

The stateless `/ask` endpoint is still useful for testing. For a real chat flow, create a session and then ask inside that session.

Create a session:

```bash
curl -X POST http://127.0.0.1:8000/chat/sessions \
  -H "Authorization: Bearer <viewer_or_higher_token>" \
  -H "Content-Type: application/json" \
  -d '{"title":"Elephant transport questions"}'
```

Ask the first question:

```bash
curl -X POST http://127.0.0.1:8000/chat/sessions/<session_id>/ask \
  -H "Authorization: Bearer <viewer_or_higher_token>" \
  -H "Content-Type: application/json" \
  -d '{"message":"What are the conditions for transport of captive elephants?"}'
```

Ask a follow-up:

```bash
curl -X POST http://127.0.0.1:8000/chat/sessions/<session_id>/ask \
  -H "Authorization: Bearer <viewer_or_higher_token>" \
  -H "Content-Type: application/json" \
  -d '{"message":"What documents are required for that?"}'
```

The app rewrites follow-up questions into standalone retrieval queries before vector search. Chat history is used only to understand references; factual answers still have to come from retrieved source chunks.

List sessions:

```bash
curl http://127.0.0.1:8000/chat/sessions \
  -H "Authorization: Bearer <viewer_or_higher_token>"
```

Get messages:

```bash
curl http://127.0.0.1:8000/chat/sessions/<session_id>/messages \
  -H "Authorization: Bearer <viewer_or_higher_token>"
```

Read audit events as an admin:

```bash
curl http://127.0.0.1:8000/admin/audit-events \
  -H "Authorization: Bearer <admin_access_token>"
```

Delete one message:

```bash
curl -X DELETE http://127.0.0.1:8000/chat/sessions/<session_id>/messages/<message_id> \
  -H "Authorization: Bearer <viewer_or_higher_token>"
```

Delete a whole session and its messages:

```bash
curl -X DELETE http://127.0.0.1:8000/chat/sessions/<session_id> \
  -H "Authorization: Bearer <viewer_or_higher_token>"
```

Try:

```json
{"question":"What details are required in a timber transit permit?"}
```

```json
{"question":"Can bamboo be sold outside the district?"}
```

```json
{"question":"What changed in the 2024 amendment?"}
```

## Deployment

Build and run locally with Docker:

```bash
docker compose up --build
```

The compose file starts both the API and Celery worker. It expects `CELERY_BROKER_URL` in `.env`; for production, point that value at Redis Cloud rather than a platform-local Redis service.

Use `deployment.env.example` as the production environment checklist. In pilot production, keep `AUTH_DISABLED=false`, set a strict `CORS_ALLOWED_ORIGINS`, set `VALIDATE_CONFIG_ON_STARTUP=true`, set `LOG_LEVEL=INFO` or stricter, store secrets in the platform secret manager, and run database migrations before deploying the API.

## Quality checks

Run these before shipping changes:

```bash
python -m ruff check .
python -m pytest -q
python -m compileall app.py auth.py auth_repository.py rag.py rag_errors.py documents.py chunking.py retrieval.py prompts.py chat_service.py ingest_service.py repositories.py task_queue.py tasks.py services routers tests
```

## Files

- `data/docs/*.pdf`, `data/docs/*.docx`, `data/docs/*.txt`, `data/docs/*.ppt`, and `data/docs/*.pptx`: local source documents when `DOCUMENT_STORAGE_BACKEND=local`
- `supabase_schema.sql`: fresh database schema, pgvector/full-text indexes, and hybrid-search RPC
- `migrations/*.sql`: incremental database changes for existing deployments
- `app.py`: FastAPI app factory, middleware, router registration, and exception handler wiring
- `routers/`: route groups for auth, admin, documents, QA, chat, and system endpoints
- `schemas.py`: request and response models used by the API
- `auth.py`: auth service logic, roles, token handling, password changes, and audit helpers
- `auth_repository.py` and `repositories.py`: Supabase persistence boundaries
- `documents.py`: source document loading and text extraction
- `chunking.py`: structure-aware chunking
- `retrieval.py`: embeddings, hybrid retrieval, and source formatting
- `prompts.py`: prompt construction and Gemini answer/rewrite calls
- `chat_service.py`: chat orchestration
- `ingest_service.py`: ingest orchestration and durable job updates
- `task_queue.py` and `tasks.py`: Celery enqueue helpers and worker task definitions
- `rag.py`: compatibility facade that re-exports RAG helpers for older imports
- `services/gemini.py`, `services/storage.py`, and `services/document_storage.py`: external service clients

## Chunking strategy

The app uses a structure-aware chunking strategy designed for rules, circulars, amendments, FAQs, long procedures, and government orders:

- Extract text per PDF page, presentation slide, DOCX paragraph/table content, or TXT file.
- Classify each document as regular section content, FAQ content, or procedure/process/workflow content.
- Detect likely headings such as `Chapter`, `Part`, `Section`, `Rule`, `Schedule`, `Annexure`, numbered headings, and all-caps headings.
- Detect likely clause starts such as `1.`, `1.1`, `(a)`, `(1)`, and roman numerals.
- Keep heading context with following clauses.
- For FAQs, group each question with its answer and repeat the FAQ section + question in every split chunk of a long answer.
- For procedures, use a larger chunk budget, stronger overlap, and a repeated procedure context line in continuation chunks.
- Split long procedural sentences on enumerated subclauses such as `(a)`, `(b)`, or `1.` before falling back to raw token splits.
- Split very long passages by sentence, preserving sentence boundaries where possible.
- Pack nearby clauses into chunks using a token budget, not a character budget.
- If a single sentence exceeds the unit budget, split it by tokens as a fallback.
- Add overlap using complete previous clauses/sentences so cross-boundary answers still retrieve enough context without starting mid-sentence.
- Store `section_heading`, `page_start`, `page_end`, `chunk_type`, and metadata with each chunk.

You can tune this in `.env`:

```text
CHUNK_TOKENS=600
CHUNK_OVERLAP_TOKENS=100
MAX_UNIT_TOKENS=220

FAQ_CHUNK_TOKENS=650
FAQ_CHUNK_OVERLAP_TOKENS=80
FAQ_UNIT_TOKENS=260

PROCEDURE_CHUNK_TOKENS=600
PROCEDURE_CHUNK_OVERLAP_TOKENS=120
PROCEDURE_UNIT_TOKENS=260

RAG_DIRECT_CANDIDATES=50
RAG_DIRECT_ANCHORS=4
RAG_DIRECT_CONTEXTS=5
RAG_DIRECT_CONTEXT_TOKENS=3000
RAG_PROCEDURE_CANDIDATES=80
RAG_PROCEDURE_ANCHORS=6
RAG_PROCEDURE_CONTEXTS=8
RAG_PROCEDURE_CONTEXT_TOKENS=5000
RAG_COMPARISON_CANDIDATES=100
RAG_COMPARISON_ANCHORS=8
RAG_COMPARISON_CONTEXTS=10
RAG_COMPARISON_CONTEXT_TOKENS=7000
RAG_OVERVIEW_CANDIDATES=120
RAG_OVERVIEW_ANCHORS=8
RAG_OVERVIEW_CONTEXTS=12
RAG_OVERVIEW_CONTEXT_TOKENS=8000
RAG_TEMPORAL_CANDIDATES=120
RAG_TEMPORAL_ANCHORS=8
RAG_TEMPORAL_CONTEXTS=12
RAG_TEMPORAL_CONTEXT_TOKENS=8000
RETRIEVAL_MAX_PER_SOURCE=0
RETRIEVAL_DUPLICATE_THRESHOLD=0.82
RETRIEVAL_EXPAND_NEIGHBORS=true
RETRIEVAL_MIN_CONTEXT_SCORE=0.0
RETRIEVAL_CONFIDENCE_THRESHOLD=0.01
RAG_MULTI_QUERY=true
RAG_MULTI_QUERY_MAX=4
RAG_EVIDENCE_PLANNING=true
RAG_ANSWER_VERIFICATION=true
GEMINI_DIRECT_MODEL=gemini-3.5-flash-lite
GEMINI_COMPLEX_MODEL=gemini-3.8-flash
GEMINI_UTILITY_MODEL=gemini-3.5-flash-lite
GEMINI_VERIFICATION_MODEL=gemini-3.5-flash-lite
GEMINI_DIRECT_THINKING_LEVEL=minimal
GEMINI_COMPLEX_THINKING_LEVEL=medium
GEMINI_COMPLEX_HIGH_THINKING_LEVEL=high
RAG_INDEX_VERSION=4
```

For this use case, the regular section chunk size is intentionally moderate. Rules and circulars often need enough context to include exceptions, amendments, and conditions, but very large chunks reduce retrieval precision. FAQ chunks are allowed a little more room because question text is repeated for context. Procedure chunks are larger and have more overlap because a complete answer often depends on neighboring steps. `MAX_UNIT_TOKENS`, `FAQ_UNIT_TOKENS`, and `PROCEDURE_UNIT_TOKENS` keep individual sentence/clause/step units manageable before they are packed into retrieval chunks. The defaults are a practical starting point, not a final production setting.

## Retrieval strategy

Retrieval is a multi-stage hybrid pipeline:

- The API embeds the user question or rewritten chat search query with Gemini. Broad overview questions are expanded into
  bounded rule, procedure, consequence, exception, and enforcement facets and embedded in one batch.
- Supabase returns a broad candidate set using pgvector similarity plus PostgreSQL English full-text search over `source`, `section_heading`, and `content`.
- A conservative deterministic reranker keeps the database hybrid score as the dominant signal, then adds small boosts for lexical overlap, exact legal identifiers, years, document titles, section headings, and question intent while penalizing likely table-of-contents noise.
- Adaptive profiles choose candidate, anchor, final-context, and token budgets based on question shape. Results suppress near-duplicate chunks, then add neighboring chunks without displacing stronger anchors.
- Low-confidence evidence causes the answer layer to abstain. Generated answers must contain valid citations or they are rejected as unsupported.

Regular `/ask` and chat requests now try a Google Search fallback after either insufficient document evidence or an
unsupported document answer (`RAG_WEB_FALLBACK=true` by default; set it to `false` to restore document-only behavior).
The fallback uses the existing Gemini API key and its own `GEMINI_WEB_MODEL` settings, through
[Gemini search grounding](https://ai.google.dev/gemini-api/docs/generate-content/google-search).
It sends the question, including retained conversational scope when available, to the provider; retrieved document
excerpts are not included. It does not run in admin legal previews or RAG Lab evaluations.

Successful web answers begin with an explicit disclosure that database evidence was insufficient and the answer uses
internet sources unverified against the database. Only text segments attributed by the search provider to usable web
sources are published, with clickable `Web N` citations. Responses add `answer_origin=web`, `web_sources`
(`citation_number`, `url`, `title`), and `web_search_entry_point`. Existing `sources` and `confidence` still describe
document retrieval; `cited_sources` is empty for web answers. `outcome=answered` and `abstained=false` describe the
successful web result, not successful database verification. Document answers default to `answer_origin=documents`.
Search failures, truncated responses, and absent usable web grounding preserve the original abstention. Provider
attribution is not an independent factual or legal audit. Web answers never enter the document answer cache.

Before scope classification, `/ask` and chat messages receive a model-free conversational check
(`conversational_routing.py`). Messages consisting entirely of approved greetings, wellbeing questions/replies,
thanks, goodbyes, introductions, identity/capability questions, availability checks, getting-started questions,
sample-question requests, compliments, apologies, general frustration/confusion, feedback, or requests for a light
joke get professional predefined replies. Most categories offer several wordings. Identity replies identify the
assistant as AI; sample questions and jokes contain no legal claims. General frustration receives a brief,
supportive acknowledgement and an invitation to explain the forestry matter.
Matching ignores capitalization, common punctuation, extra whitespace and straight/curly apostrophes;
combinations such as "Hey, what's up?" and "Thanks, bye!" are supported. Responses have
`outcome=conversational`, `answer_origin=conversational`, `abstained=false`, empty sources and no confidence score.
Clients should display these as ordinary chat text without citations. No classifier, answer model, embedding,
document retrieval, cached document answer, or web search is invoked for these messages. Chat replies use the
existing atomic turn persistence and request-ID replay, so retries return the saved wording.

The check matches the entire message and never strips a greeting off a substantive request.
"Hi, what are the tree-felling rules?" and "Thanks, who won the match?" still pass through the scope gate.
Context-dependent replies such as "yes please", "okay", and "What about on private land?" also go through that
gate with recent chat history; they are not matched as predefined pleasantries. Unrecognized small talk can receive
a generated reply from that same scope-classification call, as described below. Confusion, negative feedback,
example requests and getting-started questions also use
the normal context-aware answer path when history includes a substantive assistant response. For example,
"I don't understand" and "Give me examples" after a forestry answer refer back to that answer, even if greetings
or thanks intervened. Recent history includes message metadata to distinguish predefined replies from substantive
answers; legacy answers without metadata conservatively use the normal path.
The conversational check remains active if the scope gate is disabled; keep
`RAG_SCOPE_GATE=true` to enforce relevance for other messages. Extend the approved phrases and responses in
`conversational_routing.py` when real usage identifies missing pleasantries. Offline regression coverage is in
`tests/test_conversational_routing.py`.

Before retrieval or cached-answer reuse, remaining questions and chat messages pass a tool-free Flash-Lite scope
classification (`RAG_SCOPE_GATE=true`). It covers forestry, wildlife, conservation, related law/policy and forest-department
administration. The same call can classify an unfamiliar social message as `conversational` and return a short `reply`;
no second writing call is made. The assistant's identity, tone, forestry boundaries and one-to-two-sentence instruction
are inline in `query_scope.py` (`SCOPE_INSTRUCTION`), separate from the JSON user/history input. No separate Markdown
file, service, model configuration, or frontend response type is needed. The reply uses the same conversational API
shape as templates, bypasses document caching/retrieval and web search, and is persisted/replayed like any chat turn.

The structured routing schema requires `scope` and `reply` (empty for non-conversational routes). Only a conversational
verdict can publish generated text; text returned with other verdicts is ignored. Runtime validation limits replies to
320 characters and 60 whitespace-separated words, rejects empty/non-string content, common link/HTML/citation markup,
and embedded control characters, and uses a fixed conversational fallback if validation fails. A failed, truncated,
unparseable or unknown route returns the existing predefined clarification instead of guessing or retrying. The prompt
also asks for plain text, no legal/factual advice, no repeated introductions, no routine follow-up question, and a reply
in the user's language. These semantic requirements remain model judgments, not guarantees provided by length checks.
Messages requesting corrections/examples of a forestry answer are instructed to stay on the substantive answer path.

Unrelated or mixed unrelated requests return `outcome=out_of_scope`; unclear requests and classifier
failures return `outcome=clarification_required`. Both have `abstained=true`, empty sources and a user-facing explanation.
Chat classification receives recent history (at most 6000 characters total and 1500 per message) to resolve follow-ups
and preserves the atomic/idempotent turn flow; full answer context is still handled separately by the existing resolver.
Clients must handle these additive outcome values. Admin legal previews and Lab evaluations remain document-only.
The classification is a model judgment, not a guarantee against adversarial prompts; it needs representative live evaluation.

Default scope model: `gemini-3.5-flash-lite`, minimal thinking, 192 output tokens for the entire structured response
(including thinking), 20-second timeout. Existing `GEMINI_SCOPE_*` overrides remain respected. Template replies have
no model usage; generated social replies are accounted for under `scope_classification`, without a separate generation
stage or search charge. Offline coverage in `tests/test_generated_conversation.py` checks the real client request payload
against scripted provider responses, invalid output fallbacks, and idempotent chat replay; no paid live model evaluation
is performed by these tests. Default web model:
`gemini-3.5-flash-lite`, minimal thinking, 1200 output tokens, 45-second timeout. Neither path retries parsing/truncation
or failed generation automatically. Document-answer model configuration is independent.

Web evidence is filtered to the configured `RAG_WEB_PRIMARY_DOMAINS` host suffixes. Defaults are `gov.in`, `nic.in`,
`.gov`, `gov.uk`, `legislation.gov.uk`, `fao.org`, and `un.org`. Matching uses hostname boundaries, HTTPS and normal
TLS ports. Opaque Google citation URLs are resolved only through the fixed `vertexaisearch.cloud.google.com`
grounding redirect endpoint; the server reads its redirect Location without fetching the destination or following
redirect chains. Unsupported/unresolvable sources are excluded. At most eight unique citation URLs are considered.
Only passages with at least one approved supporting source are published. The model is instructed to use official
legislation/court/government publications for binding legal claims and to preserve jurisdiction and date; filtering
hosts does not independently verify legal applicability or currentness.

Search admission uses atomic Redis counters, shared across processes through the existing security/broker Redis:

| Setting | Default |
|---|---:|
| `RAG_WEB_USER_DAILY_CALLS` | 20 |
| `RAG_WEB_GLOBAL_DAILY_CALLS` | 200 |
| `RAG_WEB_MONTHLY_QUERIES` | 4000 |
| `RAG_WEB_DAILY_BUDGET_USD` | $5.00 gross web model + search cost |
| `RAG_WEB_RESERVED_QUERIES` | 3 per admission |
| `RAG_WEB_RESERVE_USD` | $0.10 per admission |

Daily/monthly windows use the Redis server's UTC clock. Missing user identity, exhausted capacity or Redis outage
denies web search and preserves the document abstention. Admission reserves calls, queries and cost before contacting
Gemini. Settlement reconciles reported actual queries and token/tool charges once; failed calls with unknown usage retain
their reservations and latch a day-scoped uncertainty circuit to stop further admissions. Reservations survive process
crashes and are not automatically refunded. Counter keys expire after 40 days. These are caps on **new admissions**:
Gemini's native tool has no application-enforced query maximum within an in-flight call. Its actual cost/query count
can exceed the reservation, after which further work is denied when capacity is exhausted. The prompt requests at most
three queries, but this is not a hard provider limit. These controls cannot guarantee an absolute provider spending cap.
The web budget excludes the preceding classifier/document pipeline. Tune it separately from the older cost-reporting target.

Accounting reports model tokens (including tool-use prompt and thinking tokens), unique nonempty reported search queries,
search cost, and combined estimated API cost. Search is estimated at the gross $14/1000-query rate; Google's shared
5,000-query **monthly** allowance is not deducted because other applications/models can consume it. Actual invoices
can therefore be lower. Missing/invalid usage remains explicitly unknown and never becomes zero. The complete estimate
is a usage estimate, not a billing reconciliation. Logs record codes/counts/costs, not raw queries or source text.

Clients should show the disclosure and web links, and must render the provider's Search Suggestions from
`web_search_entry_point` according to the linked provider requirements (use an isolated rendering surface for the supplied
HTML). This backend repository does not contain that frontend. Apply `migrations/016_web_fallback_metadata.sql` to retain
the structured web fields in assistant-message metadata when history is reloaded; the disclosure and links remain in
saved answer text even before that migration. Search adds latency and separate tool charges. No paid live search validation
or deployment is performed by the offline tests. Offline regressions use scripted scope verdicts and search responses;
atomic quota tests use a disposable local Redis socket (`RAG_REDIS_TESTS=1`), never the configured service.

Broad, procedural, and comparison questions receive an evidence-planning pass before final writing. The final compiler
groups related excerpts, ranks core rules ahead of adjacent material, adapts the Markdown structure to the question, and
uses short, spaced paragraphs and scannable lists instead of dense text blocks. It ends supported answers with a brief
evidence-grounded follow-up direction. Direct factual questions use Gemini 3.5 Flash-Lite with minimal thinking; procedure,
comparison, overview, amendment, conflict, and historical synthesis use Gemini 3.8 Flash with medium or high thinking.
A structured verification pass checks the draft against its cited excerpts and corrects or rejects
unsupported claims. Source responses include stable `citation_number` values; `cited_sources` contains the subset actually
cited while `sources` retains the complete context set for compatibility and debugging.

Token optimizations distinguish factual lookups (including application fees and permit durations) from procedures,
keep those lookups on Flash-Lite, shorten repeated instructions, and give concise answers without truncating qualifications.
First-message rewrites are skipped, exact repeated evidence is referenced once, and unchanged drafts receive compact
verification approvals. Rejected Lite audits get one full-evidence recheck by the complex model before abstaining. See
[token efficiency and quality validation](docs/token_efficiency.md) for usage logs, testing, and the verification rollback flag.

Gemini Embedding 2 inputs use its retrieval convention: queries are prefixed with `task: question answering | query:` and chunks use `title: ... | text: ...`. The chunk text includes document type, authority, section heading, legal identifiers, and content. `RAG_INDEX_VERSION` controls automatic reindexing when this representation changes. After deploying a new index version, run `POST /ingest`; documents indexed with an older version will be rebuilt.

`score` in source responses is the final reranked score, not raw cosine similarity. `evidence_role` is `matched` for directly retrieved chunks and `neighbor` for adjacent context.

### Latest applicable information

Ingestion stores unambiguous, explicitly labelled `issued_date` and `effective_date` values plus amendment-reference lines in the existing JSON metadata. Supported dates include ISO, day/month/year, and day month-name year. Mentioned years and upload timestamps are never treated as issue dates. Ambiguous or unrecognized dates remain unknown; amendment-reference lines are evidence for interpretation, not verified supersession links.

Current questions receive a bounded recency boost (at most 0.03) for dated, relevant candidates. Future-dated or future-effective material receives no current recency boost. Questions naming legal identifiers also run an additional hybrid amendment search before context selection, adding one embedding request and one database search. Historical questions disable the current recency preference and amendment search; a year in a rule title alone does not disable them. Date selection and precedence are still resolved by the answer model from the supplied evidence.

The answer prompt receives issue/effective dates and instructs the model to prioritize the latest applicable provision, preserve unchanged older provisions, respect historical dates, and disclose unresolved conflicts. This is a relevance-based retrieval improvement, not an exhaustive amendment registry: updates can still be missed, and newer dates alone do not prove supersession.

Existing chunks immediately benefit from the prompt and can recover explicitly labelled dates from their own text. To propagate document-level dates to every chunk, refresh each existing document with `POST /ingest` and `{"source":"<filename>"}`. No database migration is needed. An ordinary corpus ingestion may skip already-indexed documents; the explicit source refresh rebuilds them. The changes do not automatically re-ingest stored documents.

### Cost policy

See [cost policy and rollout](docs/cost_policy.md) for selective planning, verbatim history selection, per-stage USD/INR
cost estimates, the advisory ₹1.50 threshold, live quality comparisons, deployment-version checks, and rollback flags.

### Opt-in handbook-led legal verification

Existing ingested documents can use a separate, environment-scoped legal registry without re-ingestion or changes
to existing API contracts. The new retrieval path is disabled by default. Admin previews report missing evidence,
follow reviewed amendment/order relationships, and preserve the shared production corpus. See
[legal hierarchy setup, API, compatibility, and dev validation](docs/legal_hierarchy.md) before applying migration 010
or enabling the feature. Development and production must use different registry namespaces on a shared database.

### Admin document deletion

Admins can permanently delete a single library file and its search index with
`DELETE /documents/{document_id}`. Apply migration 017 before deploying the feature.
See [deletion setup, retry behavior, and frontend flow](engineering/document-deletion/README.md).
