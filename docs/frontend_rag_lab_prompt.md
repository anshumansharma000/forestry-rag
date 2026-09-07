# Frontend implementation prompt: Admin RAG Lab

Implement a production-quality **RAG Lab** area in the existing Forest Department Pilot RAG frontend. This is an admin-only workspace for uploading private test documents, tuning chunking and retrieval settings, inspecting generated chunks, running sandbox questions, and publishing an approved revision into the regular chat corpus.

Use the existing frontend architecture, authenticated API client, bearer-token handling, routing, design system, form components, notifications, error parsing, and test setup. Do not introduce a second component library or call Gemini or Supabase directly from the browser.

## Product boundary

RAG Lab is deliberately separate from the regular Documents flow:

- Uploaded experiment files and draft chunks are private sandbox material.
- They must not appear in the Documents library or regular chat before publication.
- Building a revision creates an immutable chunk/embedding snapshot.
- Retrieval settings used for sandbox questions may be changed per query without rebuilding chunks.
- Publishing copies the selected ready revision into the production corpus. The backend performs this transaction; the frontend must never imitate publication by calling regular document ingestion endpoints.
- Once publication succeeds, the sources automatically participate in regular chat retrieval.

Do not add delete, unpublish, rollback, file-download, presigned-upload, or direct database actions. Those backend capabilities do not currently exist.

## Access and navigation

- Add a navigation item named **RAG Lab** under the existing admin area.
- Render and navigate to it only when the authenticated user's role is `admin`.
- Do not expose it to `viewer`, `officer`, or `knowledge_manager` users.
- Keep the API as the authoritative permission boundary and handle `401` and `403` responses normally.
- Suggested routes:
  - `/admin/rag-lab` — experiment list
  - `/admin/rag-lab/:experimentId` — experiment workspace

## API types

Use these frontend types or equivalent generated types:

```ts
type RagLabChunkingConfig = {
  strategy: "structure_aware_v1";
  profile: "auto" | "section" | "faq" | "procedure";
  max_tokens: number;
  overlap_tokens: number;
};

type RagLabRetrievalConfig = {
  top_k: number;
  candidate_count: number;
  max_per_source: number;
  duplicate_threshold: number;
  min_context_score: number;
  expand_neighbors: boolean;
};

type RagLabConfig = {
  chunking: RagLabChunkingConfig;
  retrieval: RagLabRetrievalConfig;
};

type RagLabExperimentStatus =
  | "draft"
  | "building"
  | "ready"
  | "publishing"
  | "published"
  | "failed"
  | "archived";

type RagLabRevisionStatus =
  | "queued"
  | "building"
  | "ready"
  | "publishing"
  | "published"
  | "failed";

type RagLabExperiment = {
  id: string;
  name: string;
  description: string | null;
  status: RagLabExperimentStatus;
  owner_user_id: string | null;
  config: RagLabConfig;
  published_revision_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

type RagLabFile = {
  id: string;
  experiment_id: string;
  filename: string;
  kind: "pdf" | "docx" | "txt" | "ppt" | "pptx" | string;
  checksum_sha256: string;
  size_bytes: number;
  extraction_metadata: {
    title?: string;
    page_count?: number | null;
    document_metadata?: Record<string, unknown>;
  };
  created_at: string;
  updated_at: string;
};

type RagLabRevision = {
  id: string;
  experiment_id: string;
  revision_number: number;
  status: RagLabRevisionStatus;
  config: RagLabConfig;
  chunk_count: number;
  error: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  published_at: string | null;
};

type IngestJob = {
  id: string;
  kind: "rag_lab.build" | "rag_lab.publish" | string;
  status: "queued" | "running" | "succeeded" | "failed";
  result: Record<string, unknown> | null;
  error: string | null;
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  started_at: string | null;
  finished_at: string | null;
};

type RagLabChunk = {
  id: string;
  revision_id: string;
  file_id: string;
  source: string;
  chunk_index: number;
  chunk_type: string;
  section_heading: string | null;
  page_start: number | null;
  page_end: number | null;
  token_estimate: number;
  content?: string;
  metadata: Record<string, unknown>;
};

type RagSource = {
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

type ApiError = {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
};
```

Do not display backend-only `storage_key`, `extraction_key`, or storage paths to users.

## Experiment list

Fetch:

```http
GET /admin/rag-lab/experiments?offset=0&limit=25
```

Response:

```ts
type RagLabExperimentListResponse = {
  items: RagLabExperiment[];
  pagination: {
    offset: number;
    limit: number;
    total: number;
    has_more: boolean;
  };
};
```

Build a focused experiment list rather than an analytics dashboard:

- Header: **RAG Lab** and a short explanation that draft content is isolated from regular chat.
- Primary action: **New experiment**.
- Each row/card shows name, description, status, last updated time, and published revision when present.
- Sort order comes from the backend: most recently updated first.
- Use server pagination and show “Showing X–Y of Z experiments”.
- Clicking a row opens the experiment workspace.
- Empty state: “No RAG Lab experiments yet” with the new-experiment action.

Create an experiment with:

```http
POST /admin/rag-lab/experiments
Content-Type: application/json

{
  "name": "Forest policy tuning",
  "description": "Evaluate section-aware chunking",
  "config": { ...RagLabConfig }
}
```

The config is optional, but initialize the form with these backend defaults:

```ts
const defaultRagLabConfig: RagLabConfig = {
  chunking: {
    strategy: "structure_aware_v1",
    profile: "auto",
    max_tokens: 600,
    overlap_tokens: 100,
  },
  retrieval: {
    top_k: 5,
    candidate_count: 40,
    max_per_source: 0,
    duplicate_threshold: 0.82,
    min_context_score: 0,
    expand_neighbors: true,
  },
};
```

On success, navigate to the created experiment workspace.

## Experiment workspace

Fetch all workspace state with:

```http
GET /admin/rag-lab/experiments/{experiment_id}
```

Response:

```ts
type RagLabExperimentDetailResponse = {
  experiment: RagLabExperiment;
  files: RagLabFile[];
  revisions: RagLabRevision[];
};
```

Create one coherent workspace with four sections or tabs:

1. **Files**
2. **Configuration**
3. **Chunks**
4. **Playground**

Keep the selected revision visible in a persistent revision selector near the workspace header. Default to the newest revision. When the latest revision is building, continue showing its status and poll instead of silently switching to an older ready revision.

The header should show experiment name, description, status, and the published revision badge when applicable. Provide an edit action for name, description, and default configuration:

```http
PATCH /admin/rag-lab/experiments/{experiment_id}
Content-Type: application/json

{
  "name": "Updated name",
  "description": "Updated description",
  "config": { ...RagLabConfig }
}
```

Send only changed fields. Editing the experiment configuration affects future revisions; it must not visually imply that an existing revision was mutated.

## Files section

Upload experiment files through:

```http
POST /admin/rag-lab/experiments/{experiment_id}/files
Content-Type: multipart/form-data
```

- Use a multiple-file picker and append each file using `formData.append("files", file)`.
- Accept PDF, DOCX, TXT, PPT, and PPTX.
- Maximum 50 files per request, 150 MiB per file, and 150 MiB aggregate multipart size.
- Filenames must be unique within the experiment. Show the backend's `409` message when a duplicate is rejected.
- Do not call `/ingest` after an experiment upload. RAG Lab files are processed only when a revision is built.
- Disable upload while the experiment status is `building`, `publishing`, or `archived`.
- Show filename, file type, formatted size, checksum abbreviation, inferred title, page count, and extraction state when available.
- Explain that extraction occurs on the first build and is cached for later revisions.
- There is no delete endpoint; do not render a non-functional delete action.

After upload, refetch experiment detail. A previously ready or published experiment may return to `draft`; reflect the server status.

## Configuration section

Separate the controls into **Chunking** and **Retrieval** groups.

Chunking controls:

- Strategy: show `Structure-aware v1` as read-only because it is currently the only strategy.
- Profile: Auto, Section, FAQ, or Procedure.
- Maximum tokens: integer from 100 through 2000.
- Overlap tokens: integer from 0 through 500 and strictly smaller than maximum tokens.

Retrieval controls:

- Results (`top_k`): integer from 1 through 20.
- Candidate pool (`candidate_count`): integer from 1 through 200. Warn or automatically keep it at least as large as `top_k` for understandable behavior.
- Maximum results per source (`max_per_source`): integer from 0 through 20; explain that zero means unlimited.
- Duplicate threshold: number from 0 through 1.
- Minimum context score: number from 0 through 1.
- Expand neighboring chunks: boolean.

Use concise help text. Do not expose undocumented reranking weights or environment variables.

Provide two distinct actions:

- **Save defaults** updates the experiment through `PATCH` and affects future revisions.
- **Build revision** sends the configuration currently shown in the form as an immutable snapshot:

```http
POST /admin/rag-lab/experiments/{experiment_id}/revisions
Content-Type: application/json

{
  "config": { ...RagLabConfig }
}
```

Response:

```ts
type CreateRagLabRevisionResponse = {
  revision: RagLabRevision;
  job: IngestJob;
};
```

Require at least one uploaded file. Disable the build action while the experiment is `building`, `publishing`, or `archived`. After the request succeeds, select the new revision and poll its returned job.

## Asynchronous job polling

Build and publish operations use:

```http
GET /ingest/jobs/{job_id}
```

Response: `{ "job": IngestJob }`.

- Poll every 1.5–2 seconds while status is `queued` or `running`.
- Stop polling on `succeeded` or `failed`, component unmount, logout, route change, or superseding job.
- Do not create overlapping poll requests.
- Show queued and running progress without inventing a percentage.
- On success, refetch the complete experiment detail and relevant chunks/query history.
- On failure, show `job.error`, retain the failed revision in history, and allow the admin to adjust settings and build a new revision.
- Network failures while polling should show a reconnecting state and use bounded backoff; they must not mark the backend job as failed.

## Chunks section

For the selected revision, fetch:

```http
GET /admin/rag-lab/revisions/{revision_id}/chunks?offset=0&limit=50&include_content=true
```

Response:

```ts
type RagLabChunkListResponse = {
  items: RagLabChunk[];
  pagination: {
    offset: number;
    limit: number;
    total: number;
    has_more: boolean;
  };
};
```

- Fetch chunks only when the selected revision is `ready` or `published`; for queued/building revisions show the job state instead.
- Render a compact list/table grouped or filterable by source filename.
- Show chunk index, type, section heading, page range, token estimate, and content.
- Preserve line breaks in chunk content and allow long content to expand/collapse.
- Clearly distinguish token count from character count and page count.
- Use server pagination. Never fetch all chunks into browser memory.
- Changing the selected revision must cancel or ignore stale chunk requests.
- Empty ready revision: show “This revision contains no chunks” and treat it as an unexpected state, not a successful tuning result.

## Playground section

The playground queries only the selected sandbox revision:

```http
POST /admin/rag-lab/revisions/{revision_id}/query
Content-Type: application/json

{
  "question": "What approval is required?",
  "retrieval": { ...RagLabRetrievalConfig }
}
```

The retrieval object is optional. Send the controls currently shown in the playground so the saved trial records the exact settings used.

Response:

```ts
type RagLabQueryResponse = {
  trial: {
    id: string;
    revision_id: string;
    actor_user_id: string | null;
    question: string;
    retrieval_config: RagLabRetrievalConfig;
    answer: string;
    sources: RagSource[];
    confidence: number | null;
    abstained: boolean;
    latency_ms: number | null;
    created_at: string;
  };
  answer: string;
  sources: RagSource[];
  confidence: number;
  latency_ms: number;
};
```

Playground behavior:

- Enable querying only when the selected revision is `ready` or `published`.
- Require a non-empty question and disable duplicate submissions while pending.
- Render the answer, confidence, latency, and abstention state.
- Render citation chips using `display_source`, page range, and section heading.
- Open source evidence in an accessible side panel or expandable area showing score, matched/neighbor role, chunk index, and full source text.
- Do not add the sandbox question to regular chat history.
- Keep retrieval controls near the query composer so admins can iterate quickly without rebuilding chunks.

Fetch saved trials with:

```http
GET /admin/rag-lab/revisions/{revision_id}/queries?limit=50
```

Response: `{ "items": Array<RagLabQueryResponse["trial"]> }`.

Show newest trials first. Allow an admin to select a previous trial to inspect its exact retrieval settings, answer, sources, confidence, and latency. A **Reuse settings** action may copy a trial's retrieval settings into the current controls, but must not issue a query automatically.

## Revision history and publication

Show revision number, status, chunk count, creation time, error, and publication time. Each revision's config is immutable and should be viewable as a snapshot.

Only show the **Publish revision** action for a selected revision whose status is `ready`. Before publishing, show a confirmation dialog that states:

- The selected revision's documents will become available to regular chat.
- Draft revisions remain isolated.
- There is currently no unpublish or rollback action in this interface.
- Publishing a newer revision for the same experiment updates its production sources.

Publish with:

```http
POST /admin/rag-lab/revisions/{revision_id}/publish
```

Response: `{ "job": IngestJob }`.

Poll the job using the same job-polling utility. Disable all duplicate publish actions while the publication job is pending. Do not call `/ingest`, upload the files again, or manually add anything to the Documents library. On success, refetch experiment detail and show a clear **Published** state with the revision number and time.

## Status presentation

Use consistent labels and colors without relying on color alone:

- `draft` — Draft
- `queued` — Queued
- `building` — Building
- `ready` — Ready to test
- `publishing` — Publishing
- `published` — Published
- `failed` — Failed
- `archived` — Archived

Do not optimistically change a revision to ready or published. Those states must come from refetched backend data after job completion.

## Error and edge-case handling

Errors use the standard `ApiError` envelope. Display `error.message` and use `error.details` only for useful contextual information.

Handle at least:

- `400`: unsupported file, empty file, invalid input, or backend configuration problem.
- `401`: expired or missing authentication using the existing auth flow.
- `403`: non-admin access; show the existing forbidden state.
- `404`: experiment, revision, file artifact, or job no longer exists.
- `409`: duplicate filename, conflicting lifecycle state, querying an unready revision, or publishing a non-ready revision.
- `413`: file or multipart batch too large.
- `422`: invalid parameter bounds or overlap greater than/equal to chunk size.
- `503`: Celery broker or worker unavailable. Explain that processing cannot start and provide Retry; do not pretend the revision is queued successfully.

Always refetch server state after a mutation. If the response conflicts with stale UI state, trust the server.

## Loading, empty, and responsive states

- Use skeletons for initial experiment/detail loading.
- Preserve existing content with a subtle updating indicator during background refetches.
- Provide focused empty states for no files, no revisions, no chunks, and no saved queries.
- On narrow screens, stack configuration groups and show chunk/revision rows as readable cards.
- Keep primary actions reachable without making the page feel like a generic dashboard.
- Use semantic headings, labeled form controls, accessible dialogs and drawers, visible keyboard focus, and touch targets of at least 44px.

## Acceptance criteria

- Only admins can see or navigate to RAG Lab.
- An admin can create an experiment and upload supported files without triggering regular ingestion.
- Configuration validation mirrors backend bounds, including `overlap_tokens < max_tokens`.
- Building creates a new revision and polls the returned job through completion.
- Revision selection controls which chunks and saved trials are displayed.
- Sandbox queries use only the selected ready/published revision and show cited evidence.
- Retrieval parameters can be changed between queries without rebuilding chunks.
- Publishing requires confirmation, polls asynchronously, and never calls the regular `/ingest` endpoint.
- Draft files and chunks are never represented as available in regular chat.
- Published state is shown only after backend confirmation.
- All loading, empty, error, stale-request, responsive, and accessibility states are implemented.
- Add focused tests for role-based navigation, config validation, multipart construction, job polling cleanup, failed builds, revision switching, chunk pagination, sandbox query citations, saved-trial reuse, publish confirmation, and prevention of `/ingest` calls from RAG Lab.
