# P1 consistency changes

Apply `migrations/013_consistency.sql` after migrations 009–012 before deploying this code. This migration has not been applied to the live database by the code changes. It is transactional and repeatable, adds backend-only tables/RPCs, and preserves existing document chunks, chat messages and job records. New columns include job attempt counts, lab input file snapshots and trial outcomes. Keep existing backend table permissions; the new RPCs are restricted to `service_role`.

Drain old API/worker processes and deploy the API and workers together. Old code does not honor the new ownership checks and can still write job status or chat messages directly. No new environment variables or Celery Beat process are required. Keep the Redis/security settings from the preceding security rollout. PostgreSQL leases provide consistency; Redis leases remain capacity limits.

## Ingestion

Production-corpus ingestion acquires a database lease before downloading/extracting files. Only one production build runs at a time, including source-specific requests and command-line builds. A competing worker requeues its job rather than performing a duplicate build. This deliberately favors a simple, safe publication order over parallel corpus builds. Lab revisions use separate leases and can run alongside production ingestion within the existing Redis worker cap.

A SHA-256 fingerprint covers the extracted document and relevant chunking/index/embedding environment configuration. The fingerprint becomes active only with the atomic publication transaction. Matching content and configuration skip chunk generation, embeddings and publication, even for an explicit source refresh. Changed content or configuration rebuilds under the same document ID. Corpus scans now detect changed sources rather than skipping them solely by filename. Extraction still runs to compute the fingerprint; this is not an OCR cache. Existing documents without fingerprints rebuild once on their next ingestion. Increment `RAG_INDEX_VERSION` for semantic extraction/chunking code changes that should force rebuilding.

Operation leases last five minutes and renew every minute. Each acquisition uses a new UUID token. The database checks ownership in the same transaction as publication or status changes, preventing an expired worker from committing over a replacement. Interrupted builds may leave isolated staging revisions/chunks; these remain invisible to retrieval. Retention cleanup of abandoned staging data is a separate operational concern.

## Citation validation and answer outcomes

Citation validation now rejects the entire draft if any numeric reference is malformed or outside the supplied source range. It never silently deletes a bad reference while retaining its claim. Unambiguous spacing and duplicate labels are normalized (`[ 1, 1, 2 ]` becomes `[1, 2]`). Citation ranges and ambiguous formats are rejected. Validation runs before and after model verification. This guarantees reference integrity, not that every claim is semantically entailed by its referenced excerpt; the existing evidence/verifier controls still serve that purpose.

Question responses retain `answer`, `sources`, `cited_sources`, `confidence` and `abstained`, and add `outcome`:

| Outcome | Meaning | abstained |
| --- | --- | --- |
| `answered` | A generated answer passed the configured answer checks | false |
| `insufficient_evidence` | Retrieval did not provide sufficient reliable evidence | true |
| `unsupported_answer` | The draft could not pass support/citation checks | true |
| `not_generated` | Legal preview was requested without answer generation | false |

These outcomes appear on `/ask`, chat answers, lab query responses and legal previews. Lab trials and chat assistant metadata persist outcomes. In particular, `unsupported_answer` is now an abstention rather than a misleading successful answer. Provider/storage failures remain non-2xx HTTP errors and are not disguised as evidence insufficiency. Historical messages/trials are not retroactively reclassified. Clients that reject unknown response fields need to accept the additive `outcome` field.

## Jobs and recovery

Workers claim a durable job lease before executing. Succeeded/failed jobs are terminal and duplicate deliveries perform no work. Retryable execution failures return the job to `queued`; exhausted execution retries mark it `failed`. Waiting for another operation's capacity does not consume an execution attempt. Delayed enqueue callbacks can only annotate a queued job; they cannot turn a running/completed job back into queued or erase its result. Metadata patches merge in PostgreSQL rather than via a read-modify-write race.

Each running API process performs a recovery sweep every 60 seconds. PostgreSQL row locks coordinate sweepers across replicas. Queued jobs unchanged for at least 90 seconds and running jobs with expired database leases become eligible for redispatch. Persisted retry deadlines prevent recovery or duplicate deliveries from bypassing execution backoff. A live job lease excludes recovery even if a job's ordinary timestamp is old. Missing acknowledgements or broker publication failures can cause duplicate deliveries, which the job lease and terminal checks tolerate. The database row acts as a durable dispatch record; an enqueue error leaves it queued for recovery rather than irreversibly failed.

Recovery marks a repeatedly interrupted job failed after five claimed execution attempts; ordinary execution failures retain `CELERY_TASK_MAX_RETRIES`. A failed terminal job can be retried by submitting a new ingestion/build request. Recovery needs at least one running API process and a working database/broker. It cannot run while the entire deployment is stopped. Lease expiry means crash recovery is eventual, not immediate.

RAG Lab creates its revision and job in one transaction under the experiment lock, preventing orphan revisions and duplicate revision numbers. New revisions snapshot input file IDs. Retried builds preserve already committed chunks, duplicate batches are harmless, and mutations check both job and revision ownership. Ready/published builds are not rebuilt. Publication remains transactional and rejects an older revision when a newer revision of the experiment is already published.

## Chat retry contract

Clients should generate one UUID **before sending a logical turn**, retain it until the turn completes, and resend the same UUID and input for every retry:

```json
{
  "message": "What is the permit fee?",
  "top_k": 5,
  "request_id": "59bfbf95-f6b9-4cd8-ae23-653bc781e0bb"
}
```

The key is scoped to the owned chat session. Completed retries replay the stored response and original message IDs without retrieving or calling the model again. Reusing a key with different input returns 409. While a turn is active, another turn in that session returns 409 with `Retry-After`; clients should queue turns locally. Other sessions may run concurrently.

User and assistant messages are committed together, after generation. Generation failure leaves no half-turn in history. After a process crash, the same request can reclaim the expired lease; the old process cannot commit. If the completion committed but its HTTP response was lost, the retry returns the committed response. A process crash before completion can still require repeating a paid model call: exactly-once external model execution is not promised.

`request_id` is optional to preserve existing request compatibility. Without a client-supplied key, the server generates a new key per request: messages remain atomic, but separate HTTP retries cannot be deduplicated. The response includes the key. If a message belonging to a completed turn is deleted, its cached response is tombstoned and that key returns 410; replay does not restore deleted messages. Deleting a session cascades its turn records.

## Verification

The regular test suite covers outcomes, citation rejection, fingerprint behavior, duplicate jobs, retry state, chat replay and failure handling. The opt-in PostgreSQL suite exercises real simultaneous claims, transaction rollback, stale-writer fencing, recovery, permissions, migration reapplication and lab publication:

```sh
.venv/bin/pytest -q
RAG_POSTGRES_TESTS=1 .venv/bin/pytest -q tests/test_atomic_indexing_postgres.py
```

The integration suite creates and removes a network-isolated Docker PostgreSQL container. It never connects to the application's Supabase URL.
