# Forest Department Pilot RAG

This is a pilot-ready RAG API foundation for forest department source documents. It stores source files locally for now, extracts text from `.pdf`, `.docx`, and `.txt` files, chunks the text, creates embeddings with the Gemini API, stores chunks in Supabase Postgres + pgvector, and answers questions with citations.

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
```

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
- `knowledge_manager`: upload documents, preview chunks, and run ingestion
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
```

When R2 is enabled, uploads are written to `r2://fisrag-docs/<prefix>/<filename>`, and ingestion lists/downloads supported files from that prefix before indexing them into Supabase pgvector.

Supported formats:

- `.pdf`
- `.docx`
- `.txt`

PDF extraction works for PDFs that contain selectable text. Scanned image-only PDFs need OCR, which is intentionally outside this toy app for now.
Legacy `.doc` files are not supported yet; save or convert them as `.docx` first.

You can also upload files from Postman:

```http
POST http://127.0.0.1:8000/documents/upload
Content-Type: multipart/form-data

file=<your PDF, DOCX, or TXT file>
```

## Run

```bash
uvicorn app:app --reload
```

The API runs at:

```text
http://127.0.0.1:8000
```

Logs are emitted as JSON lines. Set `LOG_LEVEL` to control verbosity, for example `LOG_LEVEL=DEBUG uvicorn app:app --reload`.

## Postman / curl

Upload a PDF, DOCX, or TXT file:

```bash
curl -X POST http://127.0.0.1:8000/documents/upload \
  -H "Authorization: Bearer <knowledge_manager_or_admin_token>" \
  -F "file=@/absolute/path/to/document.pdf"
```

Queue indexing for any documents that have not already been indexed:

```bash
curl -X POST http://127.0.0.1:8000/ingest
```

The response includes a job id. Poll it until `status` is `succeeded` or `failed`:

```bash
curl http://127.0.0.1:8000/ingest/jobs/<job_id>
```

Ingest jobs are stored in Supabase `ingest_jobs`, so job status survives API restarts. Documents are marked with ingest status metadata while indexing runs.

Preview extracted chunks without calling the embedding API:

```bash
curl http://127.0.0.1:8000/chunks/preview
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

Use `deployment.env.example` as the production environment checklist. In pilot production, keep `AUTH_DISABLED=false`, set a strict `CORS_ALLOWED_ORIGINS`, set `VALIDATE_CONFIG_ON_STARTUP=true`, set `LOG_LEVEL=INFO` or stricter, store secrets in the platform secret manager, and run database migrations before deploying the API.

## Quality checks

Run these before shipping changes:

```bash
python -m ruff check .
python -m pytest -q
python -m compileall app.py auth.py auth_repository.py rag.py rag_errors.py documents.py chunking.py retrieval.py prompts.py chat_service.py ingest_service.py repositories.py services routers tests
```

## Files

- `data/docs/*.pdf`, `data/docs/*.docx`, and `data/docs/*.txt`: local source documents when `DOCUMENT_STORAGE_BACKEND=local`
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
- `rag.py`: compatibility facade that re-exports RAG helpers for older imports
- `services/gemini.py`, `services/storage.py`, and `services/document_storage.py`: external service clients

## Chunking strategy

The app uses a structure-aware chunking strategy designed for rules, circulars, amendments, FAQs, long procedures, and government orders:

- Extract text per PDF page, DOCX paragraph/table content, or TXT file.
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

PROCEDURE_CHUNK_TOKENS=720
PROCEDURE_CHUNK_OVERLAP_TOKENS=160
PROCEDURE_UNIT_TOKENS=260

TOP_K=3
```

For this use case, the regular section chunk size is intentionally moderate. Rules and circulars often need enough context to include exceptions, amendments, and conditions, but very large chunks reduce retrieval precision. FAQ chunks are allowed a little more room because question text is repeated for context. Procedure chunks are larger and have more overlap because a complete answer often depends on neighboring steps. `MAX_UNIT_TOKENS`, `FAQ_UNIT_TOKENS`, and `PROCEDURE_UNIT_TOKENS` keep individual sentence/clause/step units manageable before they are packed into retrieval chunks. The defaults are a practical starting point, not a final production setting.

## Retrieval strategy

Retrieval is hybrid. The API embeds the user question or rewritten chat search query with Gemini, then Supabase combines vector candidates with PostgreSQL full-text candidates over `source`, `section_heading`, and `content`. The RPC scores candidates with weighted vector similarity, text rank, reciprocal-rank fusion, and small metadata boosts for exact source/section mentions plus FAQ/procedure intent. `score` in source responses is the final hybrid score, not raw cosine similarity.
