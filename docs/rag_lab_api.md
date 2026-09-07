# Admin RAG Lab API

The RAG Lab is an admin-only workspace for testing chunking and retrieval settings without exposing draft material to regular chat. It uses separate files, extraction artifacts, chunks, embeddings, and query trials. Publishing copies one completed revision into the production `documents` and `document_chunks` corpus in a database transaction.

Run `migrations/009_rag_lab.sql` before using these endpoints. Build and publish operations use the existing Celery worker and are reported through `GET /ingest/jobs/{job_id}`.

## Workflow

### 1. Create an experiment

```http
POST /admin/rag-lab/experiments
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "name": "Forest policy tuning",
  "description": "Evaluate section-aware chunking",
  "config": {
    "chunking": {
      "strategy": "structure_aware_v1",
      "profile": "auto",
      "max_tokens": 600,
      "overlap_tokens": 100
    },
    "retrieval": {
      "top_k": 5,
      "candidate_count": 40,
      "max_per_source": 0,
      "duplicate_threshold": 0.82,
      "min_context_score": 0,
      "expand_neighbors": true
    }
  }
}
```

The entire `config` object is optional and defaults to the values above.

### 2. Upload private experiment files

```http
POST /admin/rag-lab/experiments/{experiment_id}/files
Content-Type: multipart/form-data

files=<PDF, DOCX, TXT, PPT, or PPTX>
```

Files are stored below an experiment/file UUID path. A filename must be unique only within its experiment. Extraction is cached separately so later chunk revisions do not repeat PDF parsing or OCR.

### 3. Build a revision

```http
POST /admin/rag-lab/experiments/{experiment_id}/revisions
Content-Type: application/json

{}
```

To override the experiment configuration for this revision, send `{ "config": { ... } }`. The response contains both `revision` and `job`. Poll the returned job through `GET /ingest/jobs/{job_id}`.

### 4. Inspect chunks

```http
GET /admin/rag-lab/revisions/{revision_id}/chunks?offset=0&limit=50&include_content=true
```

Only chunks belonging to that immutable revision are returned.

### 5. Query the sandbox

```http
POST /admin/rag-lab/revisions/{revision_id}/query
Content-Type: application/json

{
  "question": "What approval is required?",
  "retrieval": {
    "top_k": 5,
    "candidate_count": 60,
    "max_per_source": 2,
    "duplicate_threshold": 0.82,
    "min_context_score": 0,
    "expand_neighbors": true
  }
}
```

The retrieval override is optional. Every query saves its configuration, answer, sources, confidence, abstention state, and latency. List saved trials with:

```http
GET /admin/rag-lab/revisions/{revision_id}/queries
```

### 6. Publish

```http
POST /admin/rag-lab/revisions/{revision_id}/publish
```

Only a `ready` revision can be published. Publishing is asynchronous and transactional. Production documents receive `source_origin=rag_lab`, `corpus=admin_curated`, the file checksum, experiment/revision identifiers, and the complete `rag_recipe` configuration in metadata. Their chunks then participate in the same retrieval path used by regular chat.

## Supporting endpoints

```text
GET   /admin/rag-lab/experiments
GET   /admin/rag-lab/experiments/{experiment_id}
PATCH /admin/rag-lab/experiments/{experiment_id}
```

All RAG Lab endpoints require the `admin` role. Draft experiment chunks are never queried by `/ask` or regular chat.
