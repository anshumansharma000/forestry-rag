# P1 security rollout

This update preserves endpoint names and token-response fields. It requires a database migration before deployment and reachable Redis in production. It deliberately rejects reused refresh tokens, revoked sessions, excessive traffic, oversized requests and unsafe production configuration. It has not been applied to any live database by the code change.

## Deploy in this order

1. Ensure a real active administrator account exists and password login works. `BOOTSTRAP_ADMIN_TOKEN` is ignored in production. Do not rely on it for production recovery.
2. Back up the database and apply `migrations/012_auth_security.sql` using the migration owner. For a fresh install, run the base schema and previous required migrations, then 012. The migration is transactional and repeatable. It adds version columns, a password-change trigger and backend RPCs; it does not delete users or reset sessions on deployment. The backend service role needs its existing SELECT/INSERT/UPDATE permissions on `app_users` and `refresh_tokens`. RPC execution is restricted to `service_role`; this does not replace a separate table/RLS audit.
3. Set `APP_ENV=production`, `AUTH_DISABLED=false`, a random `JWT_SECRET_KEY` of at least 32 bytes, and `RATE_LIMIT_ENABLED=true` for API and worker processes. Render is detected as production automatically. Reuse the existing JWT signing secret if it meets the requirement; changing it signs everyone out. Known example placeholder secrets are rejected.
4. Configure `SECURITY_REDIS_URL`, or reuse `REDIS_URL` / `CELERY_BROKER_URL` (in that precedence). All replicas and workers for one deployment must share the same Redis database. Use separate databases/services for staging and production. Prefer a dedicated Redis instance with `noeviction` and suitable memory headroom; eviction or Redis data loss removes counters and leases. Use a private Redis endpoint or TLS (`rediss://`) with certificate verification. Production rejects disabled TLS verification. API startup checks Redis; runtime failures return 503 instead of bypassing limits.
5. Drain old API and worker processes and deploy both together. A maintenance window is safer than a mixed-version rollout: old APIs do not enforce JWT versions and old workers do not acquire leases or understand the new task retry keyword. The added database columns alone are compatible with old reads/writes, but old code cannot provide the new security guarantees. Finish/requeue old pending retries deliberately before cutting over.
6. Configure Uvicorn's `FORWARDED_ALLOW_IPS` with the actual trusted proxy addresses and prevent direct public access to the backend. Middleware uses ASGI's client address, never arbitrary forwarded headers. An incorrectly configured proxy causes users to share its IP quota. Do not trust every address unless the network boundary guarantees only your proxy can connect.
7. Verify login, refresh, password change/reset, one question, multipart/direct upload, job execution and 429/413 handling. Monitor 401, 429, 503 and `request_limit_lease_*` log events. Tune quotas for expected legitimate traffic.

Keep migration 012 if rolling code back; dropping its columns/functions can break running new processes. Rolling back API code removes access-token version enforcement, so treat that as a security rollback. Stop or drain updated workers before rolling back task signatures.

## Authentication behavior

Refresh rotation locks the user and refresh-token rows in a single PostgreSQL transaction. Exactly one concurrent caller can replace a token. If insertion fails, the old token remains usable. Password updates acquire the same user lock, increment `token_version`, and revoke all outstanding refresh tokens atomically. Token issuance checks the authenticated version under that lock, preventing an earlier login from creating a valid session after a concurrent reset.

New access JWTs carry `ver`. Every authenticated request checks it against the current database value; no revocation cache delays this check. Existing JWTs without `ver` are treated as version zero and continue working until the user's next password change/reset. Profile edits do not increment the version. The password-change response contains a new session using the incremented version; other sessions are rejected. A password reset requires a fresh login. Requests already authorized and running at the time of a reset can finish.

Clients should serialize refresh requests per session and atomically replace both returned tokens. Replaying an old refresh token returns 401. There is no reuse grace period or family-wide revocation; a simultaneous losing refresh request does not invalidate the winning replacement. If the rotation commits but its response is lost, the client must sign in again.

## Distributed limits

All rates use an atomic Redis sliding window of 60 seconds, with Redis server time. Identities are SHA-256 hashes in keys. HTTP errors retain the standard error envelope and include `Retry-After` for 429 and limiter outages. IP limits run before body parsing; authenticated user limits run after authorization. Login also has a normalized email-account quota.

| Bucket | Requests/IP/minute | Requests/user/minute | Global concurrent | Concurrent per identity |
| --- | ---: | ---: | ---: | ---: |
| API | 600 | 120 | — | — |
| Authentication | 20 | 10 | 4 | 2/IP |
| Generation | 120 | 20 | 8 | 2/user |
| Upload | 60 | 20 | 4 | 1/IP |
| Job submission | 60 | 10 | 4 | 1/user |

Authentication includes login, refresh, password change/reset and admin user creation. Public refresh has the IP quota; authenticated actions have the user quota. Login's user quota is per email. Generation includes `/ask`, chat questions, lab queries and legal preview. Upload includes presign/complete requests as well as multipart upload. Job submission includes ingestion, lab builds and publication. Other authorized endpoints use the API user quota. OPTIONS and GET `/health` bypass middleware protection to preserve preflight/liveness.

Override rates with `LIMIT_<BUCKET>_IP_PER_MINUTE` and `LIMIT_<BUCKET>_USER_PER_MINUTE`, using uppercase bucket names `API`, `AUTH`, `GENERATION`, `UPLOAD`, `JOB`. Override concurrency with `LIMIT_<BUCKET>_GLOBAL_CONCURRENCY` and either `_IP_CONCURRENCY` (auth/upload) or `_USER_CONCURRENCY` (generation/job). All configured limits must be positive.

Actual ingestion and lab job execution share `LIMIT_WORKER_GLOBAL_CONCURRENCY=2` across worker processes, with at most one live lease per job ID. Capacity contention and Redis outages requeue tasks without using execution-failure retries. Execution failures still use `CELERY_TASK_MAX_RETRIES`. These controls bound concurrent work and submission rate, not total queue length.

Leases renew every one-third of `LIMIT_LEASE_SECONDS` (default 120, minimum 10) and release on completion. Crashed processes lose their slots after expiry. During a prolonged network partition, process suspension or Redis restart, an existing operation may outlive its lease; this is a capacity guard, not a fencing mechanism for irreversible side effects. Atomic document publication remains the database consistency mechanism. Long-running operations should be monitored for lost leases.

## Request boundaries

- Ordinary bodies: `REQUEST_MAX_BYTES=1048576` (1 MiB).
- `/auth/` bodies: `AUTH_REQUEST_MAX_BYTES=16384` (16 KiB).
- Multipart upload bodies: existing `UPLOAD_BATCH_MAX_BYTES` plus 1 MiB for multipart framing. Per-file and batch validation still applies. Presigned file transfer goes directly to storage and retains existing completion-side validation.
- Headers: `REQUEST_HEADERS_MAX_BYTES=32768`; request path plus query: `REQUEST_TARGET_MAX_BYTES=8192`.
- Body-read deadline: `REQUEST_BODY_TIMEOUT_SECONDS=120`.
- Questions/messages: 8,000 characters; passwords: 256; refresh tokens: 1,024; upload filenames: 255.

Bodies are counted before parsing, including chunked input and incorrect Content-Length. At most 1 MiB is spooled in memory per request; larger accepted uploads spill into temporary storage and are cleaned up on completion, rejection or disconnect. Allow sufficient temporary disk capacity for configured concurrent uploads. Oversized bodies return 413, headers 431, URLs 414, slow bodies 408, and schema violations 422. A reverse proxy should also enforce suitable connection, header and upload limits: application middleware runs after the server has parsed request headers.

## Error redaction

API errors, JSON logs and newly persisted ingestion/lab failures use shared redaction in `redaction.py`. It masks configured secrets, credential fields, bearer/JWT values, URLs and common absolute local paths. Unexpected exceptions become generic durable failure messages; deliberately safe `AppError` messages remain useful. Private exception diagnostics are not copied into client responses or job records. Validation errors omit supplied values. Redaction is defense in depth, not permission to log arbitrary request bodies or document content. Historical stored error strings are not rewritten by this migration.

## Verification

Run the normal suite with `.venv/bin/pytest -q`. Opt-in integration checks use disposable local services and never the application database or Redis URL:

```sh
RAG_POSTGRES_TESTS=1 .venv/bin/pytest -q tests/test_atomic_indexing_postgres.py
RAG_REDIS_TESTS=1 .venv/bin/pytest -q tests/test_security_redis.py
```

PostgreSQL checks require Docker and `pgvector/pgvector:pg16`. Redis checks require a local `redis-server`; they use a temporary Unix socket with TCP disabled.
