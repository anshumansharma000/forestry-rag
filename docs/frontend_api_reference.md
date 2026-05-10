# Frontend API and Data Reference

Compact integration brief for the Forest Department Pilot RAG frontend.

## App Purpose

Forest Department Pilot RAG is a FastAPI backend for querying forest department source documents. It accepts `.pdf`, `.docx`, and `.txt` files, chunks them, embeds them with Gemini, stores searchable chunks in Supabase pgvector, and answers questions with cited source excerpts.

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
- Supported extensions: configured by the backend; default `.pdf`, `.txt`, `.docx`
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

- Accept only PDF/DOCX/TXT files in the file picker.
- After upload, prompt the user to add the new document to the index with `POST /ingest`.
- `path` is a backend storage path for diagnostics only; do not expose it as a user-facing document link.

### `POST /ingest`

Queues a background ingest job for previously unindexed files from the configured document storage backend. Existing indexed sources are skipped and their rows are left intact.

Request body: none.

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
- After `succeeded`, show added/skipped document counts and added chunk counts.
- Re-run after uploading new source files. Existing indexed files with the same source name are skipped.

### `GET /ingest/jobs/{job_id}`

Returns the same `job` shape as `POST /ingest`. Job rows are stored in Supabase `ingest_jobs`, so status survives API restarts.

### `GET /chunks/preview`

Previews locally extracted chunks without creating embeddings or writing to Supabase.

Requires `knowledge_manager` or `admin`.

Response:

```ts
type ChunkPreviewResponse = {
  documents: number;
  chunks: PreviewChunk[];
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
};
```

Frontend use:

- Optional admin/debug screen.
- Useful before indexing to inspect extraction quality.
- For large documents, render in a virtualized list or paginated table.

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
  score: number;
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
- `score` is rounded similarity; higher means more relevant.
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
- `ingest_jobs.status` is one of `queued`, `running`, `succeeded`, or `failed`.

## UX Recommendations

- Main screen: chat-first interface with a session sidebar, message timeline, source drawer, and small setup/indexing status area.
- Admin/setup screen: config status, document upload, ingest button, chunk preview.
- For answers, parse citations like `[1]`, `[2]` only for display affordances; the authoritative source list is the returned `sources` array in order.
- Show citation chips using `display_source`; open a side panel with `text`, page range, file name, and score.
- Keep upload/indexing controls separate from end-user chat if the app is meant for non-admin users.
- Disable ask/send when config is incomplete or while a request is in flight.
- Show a clear empty state when there are no sessions or no indexed chunks.

## Frontend Integration Prompt

Use this prompt to generate or implement the frontend:

```text
Build a production-quality frontend for the Forest Department Pilot RAG FastAPI backend.

API base URL: http://127.0.0.1:8000. Keep this configurable through an environment variable, with the local URL as the default. The browser must only call the FastAPI backend; never expose Gemini keys, Supabase service role keys, JWT signing secrets, password hashes, bootstrap tokens, or refresh-token hashes. Store the backend access token and refresh token in frontend auth state. Include Authorization: Bearer <access_token> on every endpoint except /health, /config/status, /auth/login, and /auth/refresh.

The app is a forest department RAG assistant. It should provide a chat-first interface for asking questions over indexed PDF/DOCX/TXT source documents, with citations and source excerpts. It should also include a compact admin/setup area for backend health, configuration status, document upload, index ingest, and chunk preview.

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
- POST /documents/upload as multipart/form-data field "file"; accept only .pdf, .docx, and .txt by default
- POST /ingest -> { job }
- GET /ingest/jobs/{job_id} -> { job }
- GET /chunks/preview -> { documents, chunks }
- POST /ask with { question, top_k? } -> { answer, sources }
- POST /chat/sessions with optional { title } -> ChatSession
- GET /chat/sessions -> { sessions }
- GET /chat/sessions/{session_id}/messages -> { session_id, messages }
- POST /chat/sessions/{session_id}/ask with { message, top_k? } -> { session_id, user_message, assistant_message, search_query, answer, sources }

Use these TypeScript types:
type Source = { source: string; display_source: string; page_start: number | null; page_end: number | null; chunk_index: number; score: number; text: string };
type ChatMessage = { id: string; session_id: string; role: "user" | "assistant"; content: string; sources: Source[]; metadata: Record<string, unknown>; created_at: string };
type ChatSession = { id: string; title: string | null; metadata: Record<string, unknown>; created_at: string; updated_at: string };
type IngestJob = { id: string; kind: string; status: "queued" | "running" | "succeeded" | "failed"; actor_user_id: string | null; result: Record<string, unknown> | null; error: string | null; metadata: Record<string, unknown>; created_at: string; updated_at: string; started_at: string | null; finished_at: string | null };

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
- Render assistant citations from assistant_message.sources or response.sources. Use display_source for citation labels. Show the source text in an expandable side panel/drawer with file name, page range, chunk index, and similarity score.
- Show search_query only in a debug/details view.

Admin/setup behavior:
- Show backend/config status without exposing secrets.
- Admins can create users with email, initial password, role, full name, metadata, and must_change_password.
- Admins can list users, update email/full name/role/active status/metadata, and reset passwords. Never display existing passwords.
- Allow PDF/DOCX/TXT upload via /documents/upload.
- After upload, indicate that /ingest must be run before new content is searchable.
- Provide an ingest button wired to /ingest, poll /ingest/jobs/{job_id}, and show added/skipped document and chunk counts.
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
