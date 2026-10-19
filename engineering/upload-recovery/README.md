# Confirmed recovery uploads

A stored filename is no longer treated as proof that a document was successfully
indexed. Duplicate checks distinguish these states:

| State | Behavior |
| --- | --- |
| `indexed` | Reject replacement, including an indexed document whose later refresh failed. Published chunks also establish this state. |
| `processing` | Reject while a source-specific or corpus ingestion job is queued/running. |
| `failed` | Require explicit confirmation before replacing the stored file. |
| `unindexed` | Require confirmation; includes uploaded files with no document row after an early failure. |

If the stored file is correct, the existing ingestion retry remains available and
does not require another upload. The new path handles corrected/replacement files.

## API contract

Requests without confirmation retain the safe default: a duplicate returns 409.
The error contains `details.conflicts`, with zero-based request `index`, normalized
`filename`, `state`, `can_replace_failed`, and `requires_confirmation` per duplicate.
Top-level `details.can_replace_failed` is true only if every conflict is recoverable.
The frontend must not offer an overwrite for indexed/processing or mixed blocked
batches, nor infer permission by matching words in an old error message.

After confirmation, retry the presign request with `replace_failed: true` only on
the confirmed entries:

```json
{
  "files": [{
    "filename": "failed.pdf",
    "size_bytes": 1200,
    "content_type": "application/pdf",
    "replace_failed": true
  }]
}
```

Presigned entries include `replace_failed: true` for those entries. Carry it into
the corresponding `/documents/uploads/complete` entry, together with any optional
categorization. Presign and completion both check current eligibility; approval
does not bypass a successful index or a job that started after the warning.
Signed PUTs continue to target the isolated staging prefix.

Single and batch multipart uploads accept the optional form field
`replace_failed=true`. A batch confirmation applies to that submitted batch, but
an indexed or processing document still blocks the batch before any file is written.
Ordinary request/response shapes remain valid. The former global
`ALLOW_DOCUMENT_REPLACE` setting no longer bypasses duplicate protection.

Finalization/replacement shares the existing `documents:ingest` lease with workers
so no worker reads a source while its bytes change. A busy lease returns the normal
retry-shortly conflict. No source chunks or document rows are deleted by recovery;
successful ingestion uses the existing atomic revision-publication path. Manual
categorization remains intact unless explicitly changed by the upload.

The existing frontend upload flow queues ingestion after successful completion.
Recovery batches serialize finalization and finish all replacements before queuing
any ingestion jobs, so a worker for one file cannot prevent the next replacement.
Cancelling the warning uploads nothing, queues nothing, and retains file selections
and metadata. A rejection at completion does not queue ingestion.

## Deployment and verification

This change requires no additional database migration. It uses the document/job
tables and ingestion leases already required by the backend (migration 013).
Deploy the API and frontend together to expose the confirmation flow. Older clients
continue receiving duplicate conflicts; no duplicate is silently overwritten.

Backend coverage is in `tests/test_upload_recovery.py`, including protection of
published evidence, active jobs, multipart/direct paths, mixed batches, confirmation
flags, lease checks, and eligibility changes between presign and completion.
Frontend coverage is in `lib/upload-replacement.test.ts`. Browser checks use mocked
storage/backend responses and never modify the real document collection.
