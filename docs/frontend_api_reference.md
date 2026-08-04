# Frontend API and Data Reference

Compact integration brief for the Forest Department Pilot RAG frontend.

## App Purpose

Forest Department Pilot RAG is a FastAPI backend for querying forest department source documents. It accepts `.pdf`, `.docx`, `.txt`, `.ppt`, and `.pptx` files, chunks them, embeds them with Gemini, stores searchable chunks in Supabase pgvector, and answers questions with cited source excerpts.

Frontend clients should treat this backend as the only API surface. Do not call Gemini or Supabase directly from the browser. The Supabase service role key must stay server-side.

All endpoints except `GET /health`, `GET /config/status`, `POST /auth/login`, and `POST /auth/refresh` require:

```http
Authorization: Bearer <access_token>
```

Roles:

- `viewer`: chat and own session history
- `officer`: reserved for officer-specific pilot workflows
- `knowledge_manager`: document upload, chunk preview, index rebuild
- `admin`: user creation, audit log access, runtime validation

Errors now use this envelope:

```ts
type ApiError = {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
};
```

## Runtime

- Local API base URL: `http://127.0.0.1:8000`
- API framework: FastAPI
- Default response format: JSON
- Upload format: `multipart/form-data`
- Error format:

```json
{
  "error": {
    "code": "auth_error",
    "message": "Bearer token is required",
    "details": {}
  }
}
```

Common failure cases:

- Missing/invalid `.env` values return `400`.
- Missing/invalid bearer token returns `401`.
- Insufficient role permissions return `403`.
- Empty or malformed request fields usually return `422` validation errors.
- Oversized uploads return `413`.
- Duplicate filenames return `409` unless replacement is enabled server-side.
- Unsupported upload type returns `400`.
- Retrieval/answer endpoints require configured Gemini and Supabase.
- Ask/chat results require source documents to have been indexed through a completed `POST /ingest` job.

## Core Flow

1. Check backend availability with `GET /health`.
2. Check runtime setup with `GET /config/status`.
3. Upload source documents with `POST /documents/upload`, or rely on files already in the configured document storage backend.
4. Queue indexing with `POST /ingest`, then poll `GET /ingest/jobs/{job_id}` until the job succeeds or fails.
5. Use either:
   - Stateless Q&A: `POST /ask`
   - Stateful chat: create a session, then use `POST /chat/sessions/{session_id}/ask`

The preferred product UX is stateful chat because the backend rewrites follow-up messages into standalone retrieval queries using recent chat history.

## Endpoints

### `GET /health`

Checks whether the API process is alive.

Response:

```json
{
  "status": "ok"
}
```

Frontend use:

- Show online/offline status.
- Use as a lightweight boot check.

### `GET /config/status`

Checks whether required runtime configuration is present without exposing secrets.

Response:

```ts
type ConfigStatus = {
  gemini_api_key_configured: boolean;
  supabase_url_configured: boolean;
  supabase_service_role_key_configured: boolean;
  supabase_url_valid: boolean;
  supabase_url_hint: string | null;
  embedding_dimensions: number;
  auth_disabled: boolean;
  bootstrap_admin_token_configured: boolean;
  jwt_secret_key_configured: boolean;
  document_storage_backend: "local" | "r2";
  r2_bucket_configured: boolean;
};
```

Frontend use:

- Show an admin/setup banner when any required value is missing.
- Display `supabase_url_hint` when `supabase_url_valid` is false.
- Expected embedding dimension is currently `768`.

### `POST /documents/upload`

Uploads a source document into the configured document storage backend. Local development defaults to `data/docs/`; R2 deployments return an `r2://...` diagnostic path.

Request:

- `Content-Type: multipart/form-data`
- Field: `file`
- Supported extensions: configured by the backend; default `.pdf`, `.txt`, `.docx`, `.ppt`, `.pptx`
- Maximum file size: 150 MiB
- Requires `knowledge_manager` or `admin`

Response:

```ts
type UploadDocumentResponse = {
  status: "ok";
  filename: string;
  path: string;
};
```

Frontend use:

- Accept only PDF/DOCX/TXT/PPT/PPTX files in the file picker.
- After upload, queue only the returned filename with `POST /ingest` and `{ "source": filename }`.
- `path` is a backend storage path for diagnostics only; do not expose it as a user-facing document link.

### `POST /documents/uploads`

Uploads multiple source documents into the configured document storage backend. Use this with a file input that has the `multiple` attribute.

Request:

- `Content-Type: multipart/form-data`
- Field: `files`
- Send one `files` part per selected file
- Supported extensions: configured by the backend; default `.pdf`, `.txt`, `.docx`, `.ppt`, `.pptx`
- Maximum file size: 150 MiB
- Maximum files per batch: 50
- Maximum aggregate multipart request size: 150 MiB
- Requires `knowledge_manager` or `admin`

Response:

```ts
type UploadDocumentsResponse = {
  status: "ok";
  files: UploadDocumentResponse[];
};
```

Frontend use:

- Append every selected file with `formData.append("files", file)`.
- Treat the upload as a single batch; duplicate filenames in the same request return `409`.
- Queue one ingest job per returned filename with `POST /ingest` and `{ "source": filename }`.

### `POST /documents/uploads/presign`

Creates short-lived R2 presigned `PUT` URLs for direct browser uploads. This endpoint only works when `DOCUMENT_STORAGE_BACKEND=r2`.

Request:

```ts
type CreatePresignedUploadsRequest = {
  files: {
    filename: string;
    size_bytes: number;
    content_type?: string | null;
  }[];
};
```

Response:

```ts
type PresignedUploadsResponse = {
  status: "ok";
  uploads: {
    upload_id: string;
    filename: string;
    upload_url: string;
    method: "PUT";
    headers: Record<string, string>;
    expires_in_seconds: number;
    max_bytes: number;
  }[];
};
```

Frontend use:

- Send one entry per selected file before uploading bytes, with at most 50 files per batch and 150 MiB per file.
- Use each returned `upload_url` with `fetch(upload_url, { method: "PUT", headers, body: file })`.
- The `Content-Type` header must exactly match the returned `headers["Content-Type"]`.
- Do not call `POST /ingest` yet; direct uploads are staged until completed.

### `POST /documents/uploads/complete`

Finalizes direct R2 uploads after the browser has successfully uploaded every file. The backend verifies the staged object, copies it into the ingestable R2 prefix, deletes the staged object, and writes the audit event.

Request:

```ts
type CompleteDirectUploadsRequest = {
  files: {
    upload_id: string;
    filename: string;
  }[];
};
```

Response: same as `POST /documents/uploads`.

Frontend use:

- Call this only after every direct `PUT` request succeeds.
- After completion succeeds, queue one ingest job per returned filename with `POST /ingest` and `{ "source": filename }`.
- If completion fails, show the backend error. The staged object may have expired, been rejected for size, or conflicted with an existing filename.

### `GET /documents`

Returns indexed documents by default, or failed ingestion files when `status=failed`. All authenticated roles may use this endpoint. Search, filtering, sorting, counting, and pagination happen on the server; the frontend must not fetch the entire corpus.

Query parameters:

```ts
type DocumentLibraryQuery = {
  status?: "indexed" | "failed"; // Defaults to indexed
  search?: string; // Searches filename and inferred title; maximum 200 characters
  kind?: "pdf" | "docx" | "txt" | "ppt" | "pptx";
  document_type?: string; // Examples: rules, act, guidelines, circular
  year?: string; // Four-digit year from 1900 through 2099
  sort_by?: "updated_at" | "created_at" | "title" | "source" | "page_count";
  sort_order?: "asc" | "desc";
  offset?: number; // Default 0
  limit?: number; // Default 25, maximum 100
};
```

Response:

```ts
type DocumentLibraryResponse = {
  items: {
    id: string;
    filename: string;
    title: string;
    kind: string;
    page_count: number | null;
    document_type: string;
    authority: string | null;
    years: string[];
    chunk_count: number;
    status: "indexed" | "failed";
    ingest_error: string | null;
    retryable: boolean;
    ingested_at: string | null;
    created_at: string | null;
    updated_at: string | null;
  }[];
  pagination: {
    offset: number;
    limit: number;
    total: number;
    has_more: boolean;
  };
};
```

Example:

```http
GET /documents?search=forest&kind=pdf&sort_by=updated_at&sort_order=desc&offset=0&limit=25
Authorization: Bearer <access_token>
```

Frontend use:

- Provide Indexed and Failed status filters. Failed rows should show `ingest_error` and a **Retry processing** action when `retryable` is true.
- Retry by calling `POST /ingest` with `{ "source": filename }`; do not upload the file again.
- Debounce search by approximately 300 milliseconds.
- Reset `offset` to `0` whenever search, filters, or sorting changes.
- Use `pagination.total` for the page count and `pagination.has_more` for the next-page state.
- Display `ingested_at` as the indexing date. The backend does not currently expose the original upload timestamp.
- `page_count` may be `null` for DOCX and TXT documents.
- Do not display a summary placeholder; summaries are intentionally not part of this contract.

### `POST /ingest`

Queues a background ingest job. The normal upload flow must set `source` so the worker downloads, extracts, chunks, and indexes only that document. Omitting the body retains the legacy corpus-wide maintenance operation; do not use the corpus-wide form after each upload.

Recommended request body:

```ts
type IngestRequest = {
  source?: string | null;
};
```

```json
{
  "source": "forest-rules.pdf"
}
```

The `source` must be the exact sanitized filename returned by the upload completion endpoint. An omitted body queues the backward-compatible corpus-wide ingest operation.

Requires `knowledge_manager` or `admin`.

Response:

```ts
type IngestResponse = {
  job: {
    id: string;
    kind: "documents.ingest";
    status: "queued" | "running" | "succeeded" | "failed";
    actor_user_id: string | null;
    created_at: string;
    updated_at: string;
    started_at: string | null;
    finished_at: string | null;
    metadata: Record<string, unknown>;
    result: null | {
      documents: number;
      documents_added: number;
      documents_skipped: number;
      chunks: number;
      chunks_added: number;
      storage: "supabase_pgvector";
    };
    error: string | null;
  };
};
```

Frontend use:

- Treat as a long-running admin action and poll `GET /ingest/jobs/{job_id}`.
- Disable the ingest button while the latest job is queued or running.
- For multi-file uploads, create and track one job per completed filename; do not queue a corpus-wide job.
- `job.metadata.source` identifies a document-scoped job and `job.metadata.scope` is `document` or `corpus`.
- After `succeeded`, show added/skipped document counts and added chunk counts.
- Re-run after uploading new source files. Existing indexed files with the same source name are skipped.

### `GET /ingest/jobs/{job_id}`

Returns the same `job` shape as `POST /ingest`. Job rows are stored in Supabase `ingest_jobs`, so status survives API restarts.

### `GET /ingest/worker/status`

Checks whether the API broker is reachable and at least one Celery consumer responds.

```ts
type IngestWorkerStatus = {
  status: "ok" | "unavailable";
  broker_configured: boolean;
  broker_reachable: boolean;
  workers_online: number;
};
```

Call this from the knowledge-manager diagnostics view. `POST /ingest` returns `503` instead of creating a queued job when `workers_online` is zero.

### `GET /chunks/preview`

Previews locally extracted chunks without creating embeddings or writing to Supabase.

Requires `knowledge_manager` or `admin`.

Response:

```ts
type ChunkPreviewResponse = {
  documents: number;
  documents_processed: number;
  chunks: PreviewChunk[];
  chunks_returned: number;
  chunks_seen: number;
  offset: number;
  limit: number;
  has_more: boolean;
  source: string | null;
  all_sources: boolean;
  include_content: boolean;
  max_content_chars: number;
};

type PreviewChunk = {
  source: string;
  chunk_index: number;
  chunk_type: "heading" | "section" | string;
  section_heading: string | null;
  page_start: number | null;
  page_end: number | null;
  content: string;
  token_estimate: number;
  metadata: {
    kind: "pdf" | "docx" | "txt" | string;
    title: string;
    [key: string]: unknown;
  };
  content_chars: number;
  content_omitted?: boolean;
  content_truncated?: boolean;
};
```

Query params:

- `limit`: page size, default `50`, maximum `200`.
- `offset`: chunk offset, default `0`.
- `source`: exact source filename. This is the normal production path and should be set to the uploaded/selected document filename.
- `all_sources`: default `false`. Set `true` only for advanced corpus-wide debugging. Requests without `source` are rejected unless `all_sources=true`.
- `include_content`: default `false`. When false, `content` is empty and `content_omitted` is true.
- `max_content_chars`: per-chunk content cap when `include_content=true`, default `500`, maximum `5000`.

Frontend use:

- Optional admin/debug screen.
- Useful before indexing to inspect extraction quality.
- After upload, preview the returned `filename` with `source=<filename>`.
- In document detail/debug views, preview the selected document only.
- Do not call this endpoint on admin page load without a source.
- Always page with `limit` and `offset`; use `has_more` to request the next page.
- Request `include_content=true` only for a selected source or small page of chunks.

### `POST /ask`

Stateless question answering.

Requires `viewer` or higher.

Request:

```ts
type AskRequest = {
  question: string;
  top_k?: number | null;
};
```

Response:

```ts
type AskResponse = {
  answer: string;
  sources: Source[];
  confidence: number;
  abstained: boolean;
};
```

Frontend use:

- Use for quick one-off questions.
- Prefer chat endpoints for conversation UX.
- `top_k` overrides the default retrieval count. Default is `TOP_K`, currently `3`.

### `POST /chat/sessions`

Creates a chat session.

Requires `viewer` or higher. Sessions are scoped to the authenticated user.

Request:

```ts
type CreateChatSessionRequest = {
  title?: string | null;
};
```

Request body may be omitted. Default title is `New chat`.

Response:

```ts
type ChatSession = {
  id: string;
  user_id: string;
  title: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};
```

Frontend use:

- Create on first message if no active session exists.
- Use `id` for subsequent message sends and history fetches.

### `GET /chat/sessions`

Lists recent chat sessions.

Response:

```ts
type ChatSessionsResponse = {
  sessions: ChatSession[];
};
```

Backend behavior:

- Returns up to 20 sessions.
- Returns only sessions owned by the authenticated user.
- Ordered by `updated_at` descending.

Frontend use:

- Populate the chat sidebar/history list.
- Show `title` with a fallback such as `Untitled chat`.

### `GET /chat/sessions/{session_id}/messages`

Loads messages for one session.

Response:

```ts
type ChatMessagesResponse = {
  session_id: string;
  messages: ChatMessage[];
};
```

Frontend use:

- Hydrate a selected chat session.
- Render assistant citations from `message.sources`.

### `POST /chat/sessions/{session_id}/ask`

Adds a user message, retrieves relevant chunks, generates an answer, stores both messages, and returns the result.

Request:

```ts
type ChatAskRequest = {
  message: string;
  top_k?: number | null;
};
```

Response:

```ts
type ChatAskResponse = {
  session_id: string;
  user_message: ChatMessage;
  assistant_message: ChatMessage;
  search_query: string;
  answer: string;
  sources: Source[];
  confidence: number;
  abstained: boolean;
};
```

Frontend use:

- Optimistically render the user's message, then reconcile with `user_message`.
- Render the assistant answer from `assistant_message.content` or `answer`.
- Use `sources` or `assistant_message.sources` for citations.
- `search_query` is useful for debug/admin UI; it is the standalone query generated for retrieval.

## Shared Data Types

```ts
type Source = {
  source: string;
  display_source: string;
  page_start: number | null;
  page_end: number | null;
  chunk_index: number;
  section_heading: string | null;
  score: number;
  evidence_role: "matched" | "neighbor";
  text: string;
};

type ChatMessage = {
  id: string;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  sources: Source[];
  metadata: Record<string, unknown>;
  created_at: string;
};

type ChatSession = {
  id: string;
  user_id: string;
  title: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};
```

## Auth and Admin Endpoints

### `GET /config/validate`

Requires `admin`. Returns `{ ok, missing, status }`; `missing` lists incomplete runtime settings.

### `POST /admin/users`

Requires `admin`. Creates a user with an initial password. The backend stores only an Argon2 password hash; passwords are never returned.

```ts
type CreateUserRequest = {
  email: string;
  password: string;
  role: "viewer" | "officer" | "knowledge_manager" | "admin";
  full_name?: string | null;
  metadata?: Record<string, unknown> | null;
  must_change_password?: boolean;
};
```

### `POST /auth/login`

Public endpoint. Logs in with email and password.

```ts
type AuthTokenResponse = {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  expires_at: string;
  refresh_expires_at: string;
  user: {
    id: string;
    email: string;
    full_name: string | null;
    role: "viewer" | "officer" | "knowledge_manager" | "admin";
    must_change_password: boolean;
  };
};
```

### `GET /auth/me`

Requires `viewer` or higher. Returns the authenticated user's public profile fields. This endpoint remains available when `must_change_password` is true.

### `POST /auth/refresh`

Public endpoint. Accepts `{ refresh_token: string }`, revokes the old refresh token, and returns a fresh `AuthTokenResponse`.

### `POST /auth/change-password`

Requires `viewer` or higher. Accepts `{ current_password, new_password }`. This endpoint remains available when `must_change_password` is true. On success, it revokes existing refresh tokens and returns a fresh token bundle plus `{ changed: true }`.

### `PATCH /auth/me`

Requires `viewer` or higher. Users may update `full_name`; they may not change their own email.

### `GET /admin/users?limit=100`

Requires `admin`. Lists users without password hashes.

### `PATCH /admin/users/{user_id}`

Requires `admin`. Admins may update email, full name, role, active status, and metadata.

### `POST /admin/users/{user_id}/reset-password`

Requires `admin`. Accepts `{ new_password, must_change_password?: boolean }`. Resets the password, revokes active refresh tokens, and never returns the password.

### `GET /admin/audit-events?limit=100`

Requires `admin`. Returns recent audit events for uploads, ingestion, user creation, and destructive chat actions.

Notes:

- All IDs are UUID strings.
- Timestamps are ISO strings from Supabase/Postgres.
- `score` is the rounded post-reranking retrieval score; higher means more relevant.
- `evidence_role` distinguishes directly matched chunks from neighboring context.
- `confidence` summarizes evidence strength. When `abstained` is true, the backend intentionally declined to answer because evidence or citations were insufficient.
- `display_source` already includes page labels, such as `file.pdf, page 4`.
- `source` is the original file name.
- `text` is the retrieved chunk excerpt. Use it in expandable citation panels.
- `metadata.search_query` exists on assistant chat messages created by `chat_ask`.

## Database Model

The frontend does not query these tables directly, but the data explains API shapes.

```ts
type DocumentRow = {
  id: string;
  source: string;
  kind: string;
  title: string | null;
  page_count: number | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

// Document metadata may include ingest_status, ingest_started_at, ingest_updated_at,
// and chunk counts from the latest ingest attempt.

type DocumentChunkRow = {
  id: string;
  document_id: string;
  source: string;
  chunk_index: number;
  chunk_type: string;
  section_heading: string | null;
  page_start: number | null;
  page_end: number | null;
  content: string;
  token_estimate: number;
  metadata: Record<string, unknown>;
  created_at: string;
};

type ChatSessionRow = ChatSession;
type ChatMessageRow = ChatMessage;

type IngestJobRow = {
  id: string;
  kind: "documents.ingest" | string;
  status: "queued" | "running" | "succeeded" | "failed";
  actor_user_id: string | null;
  result: Record<string, unknown> | null;
  error: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
};
```

Important constraints:

- `documents.source` is unique.
- `document_chunks` are unique by `(source, chunk_index)`.
- `chat_messages.role` is either `user` or `assistant`.
- `document_chunks.embedding` is `vector(768)`.
- Retrieval uses both pgvector cosine candidates and PostgreSQL full-text candidates over source, section heading, and chunk content, with small metadata boosts.
- `ingest_jobs.status` is one of `queued`, `running`, `succeeded`, or `failed`.

## UX Recommendations

- Main screen: chat-first interface with a session sidebar, message timeline, source drawer, and small setup/indexing status area.
- Admin/setup screen: config status, document upload, ingest button, chunk preview.
- For answers, parse citations like `[1]`, `[2]` only for display affordances; the authoritative source list is the returned `sources` array in order.
- Show citation chips using `display_source`; open a side panel with `text`, page range, file name, and retrieval score.
- Keep upload/indexing controls separate from end-user chat if the app is meant for non-admin users.
- Disable ask/send when config is incomplete or while a request is in flight.
- Show a clear empty state when there are no sessions or no indexed chunks.

## Frontend Integration Prompt

Use this prompt to generate or implement the frontend:

```text
Build a production-quality frontend for the Forest Department Pilot RAG FastAPI backend.

API base URL: http://127.0.0.1:8000. Keep this configurable through an environment variable, with the local URL as the default. The browser must only call the FastAPI backend; never expose Gemini keys, Supabase service role keys, JWT signing secrets, password hashes, bootstrap tokens, or refresh-token hashes. Store the backend access token and refresh token in frontend auth state. Include Authorization: Bearer <access_token> on every endpoint except /health, /config/status, /auth/login, and /auth/refresh.

The app is a forest department RAG assistant. It should provide a chat-first interface for asking questions over indexed PDF/DOCX/TXT/PPT/PPTX source documents, with citations and source excerpts. It should also include a compact admin/setup area for backend health, configuration status, document upload, index ingest, and chunk preview.

Implement these API calls:
- GET /health -> { status: "ok" }
- GET /config/status -> { gemini_api_key_configured, supabase_url_configured, supabase_service_role_key_configured, supabase_url_valid, supabase_url_hint, embedding_dimensions, auth_disabled, bootstrap_admin_token_configured, jwt_secret_key_configured }
- GET /config/validate -> { ok, missing, status } admin only
- GET /auth/me -> current user, viewer or higher
- POST /auth/login with { email, password } -> AuthTokenResponse
- POST /auth/refresh with { refresh_token } -> rotated AuthTokenResponse
- POST /auth/change-password with { current_password, new_password } -> AuthTokenResponse & { changed: true }
- PATCH /auth/me with { full_name? } -> current user; users cannot change their own email
- POST /admin/users -> creates users with initial password, admin only
- GET /admin/users -> list users, admin only
- PATCH /admin/users/{user_id} -> update email/full_name/role/is_active/metadata, admin only
- POST /admin/users/{user_id}/reset-password -> reset password, admin only
- GET /admin/audit-events -> recent audit events, admin only
- POST /documents/uploads/presign with { files: [{ filename, size_bytes, content_type }] } -> { status, uploads }
- PUT each file directly to its returned upload_url using exactly the returned headers; do not attach the API bearer token to this R2 request
- POST /documents/uploads/complete with { files: [{ upload_id, filename }] } -> { status, files }
- POST /ingest with { source: filename } -> { job }; queue one job per completed file
- GET /ingest/worker/status -> { status, broker_configured, broker_reachable, workers_online }
- GET /ingest/jobs/{job_id} -> { job }
- GET /chunks/preview -> { documents, chunks }
- POST /ask with { question, top_k? } -> { answer, sources }
- POST /chat/sessions with optional { title } -> ChatSession
- GET /chat/sessions -> { sessions }
- GET /chat/sessions/{session_id}/messages -> { session_id, messages }
- POST /chat/sessions/{session_id}/ask with { message, top_k? } -> { session_id, user_message, assistant_message, search_query, answer, sources }

Use these TypeScript types:
type Source = { source: string; display_source: string; page_start: number | null; page_end: number | null; chunk_index: number; section_heading: string | null; score: number; evidence_role: "matched" | "neighbor"; text: string };
type ChatMessage = { id: string; session_id: string; role: "user" | "assistant"; content: string; sources: Source[]; metadata: Record<string, unknown>; created_at: string };
type ChatSession = { id: string; title: string | null; metadata: Record<string, unknown>; created_at: string; updated_at: string };
type IngestJob = { id: string; kind: string; status: "queued" | "running" | "succeeded" | "failed"; actor_user_id: string | null; result: Record<string, unknown> | null; error: string | null; metadata: Record<string, unknown>; created_at: string; updated_at: string; started_at: string | null; finished_at: string | null };
type PresignedUpload = { upload_id: string; filename: string; upload_url: string; method: "PUT"; headers: Record<string, string>; expires_in_seconds: number; max_bytes: number };

type AuthUser = { id: string; email: string; full_name: string | null; role: "viewer" | "officer" | "knowledge_manager" | "admin"; must_change_password: boolean };
type AuthTokenResponse = { access_token: string; refresh_token: string; token_type: "bearer"; expires_at: string; refresh_expires_at: string; user: AuthUser };

Auth behavior:
- Show an email/password login screen when no token is present.
- On successful /auth/login, store access_token, refresh_token, expires_at, refresh_expires_at, and user.
- On load, call /health and /config/status. If a token exists, call /auth/me before loading user-scoped data.
- If user.must_change_password is true, route to a forced password-change screen and block normal app navigation until /auth/change-password succeeds. Replace stored tokens with the returned token bundle.
- Refresh the access token before expires_at using /auth/refresh and replace both access_token and refresh_token because refresh tokens rotate.
- On 401, clear auth state and show login.
- On 403 with "Password change is required", route to the forced password-change screen.

Chat behavior:
- After authenticated user hydration, call /chat/sessions.
- If there is no active session, create one when the user sends the first message.
- Send chat messages to /chat/sessions/{session_id}/ask, not /ask, for normal conversation.
- Optimistically show the user's message while waiting, then reconcile with returned user_message and assistant_message.
- Render assistant citations from assistant_message.sources or response.sources. Use display_source for citation labels. Show the source text in an expandable side panel/drawer with file name, page range, chunk index, and retrieval score.
- Show search_query only in a debug/details view.

Admin/setup behavior:
- Show backend/config status without exposing secrets.
- Admins can create users with email, initial password, role, full name, metadata, and must_change_password.
- Admins can list users, update email/full name/role/active status/metadata, and reset passwords. Never display existing passwords.
- Use the presign -> direct R2 PUT -> complete flow for PDF/DOCX/TXT/PPT/PPTX uploads. Do not send large file bytes through the FastAPI multipart endpoints.
- Track upload progress separately from processing progress. The direct R2 PUT does not include the API Authorization header and must use exactly the Content-Type returned by /presign.
- After /complete succeeds, call /ingest once per returned filename with { source: filename }. Never call the empty-body corpus-wide ingest operation after a routine upload.
- Track and poll every returned job id through /ingest/jobs/{job_id}; show queued, processing, succeeded, and failed states per file, including job.error and added chunk counts.
- Provide a chunk preview view using /chunks/preview for debugging extraction quality.

Error/loading behavior:
- Display error.message from { error: { code, message, details } }.
- Handle 401 by sending the user back to sign-in/token entry.
- Handle 403 by hiding or disabling role-restricted controls.
- Prefer `error.message` from the structured error envelope.
- Disable send/upload/ingest buttons while their requests are pending.
- Validate non-empty question/message before sending.
- Handle 400 responses for missing config and indexing/retrieval errors, 409 for duplicate filenames, 413 for oversized uploads, and 422 for request validation errors.

Design:
- Quiet operational UI, not a marketing landing page.
- First screen should be the usable chat workspace.
- Use a left session sidebar, central message timeline, bottom composer, and right citation/source drawer or responsive modal.
- Include compact admin controls in a settings panel or top toolbar.
- Keep dense information scannable, with restrained styling and clear empty/loading/error states.
```
