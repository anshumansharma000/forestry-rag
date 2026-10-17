# Local implementation validation — 16 October 2026

Priorities 1–6 are implemented locally with independent feature controls and rollback settings. The release package is ready for review and representative evaluation. Production quality, cost savings and end-to-end latency gains have not been established.

## Checks completed

| Check | Result | Evidence |
|---|---|---|
| Complete suite, including disposable PostgreSQL and isolated Redis | 464 passed, no skips; one existing Supabase dependency deprecation warning | [validation.txt](validation.txt) |
| Lint for changed and new Python files | Passed | [lint.txt](lint.txt) |
| Additive migrations | Applied repeatedly in disposable PostgreSQL; version invalidation, rollback, scope and neighbor helpers tested | `tests/test_pipeline_postgres.py` |
| Cache isolation, failure handling, lock expiry/fencing and limits | Passed with isolated Redis | `tests/test_pipeline_redis.py`, `tests/test_answer_pipeline.py` |
| Synthetic policy replay | 12 cases across nine variants, 108 runs; baseline passed 11/12 content checks, combined passed 12/12 | [replay-results.json](replay-results.json) |
| Synthetic extraction pilot | Known fee, unit, qualification and Hindi presence checks passed | [extraction-pilot.json](extraction-pilot.json) |
| Controlled cache/concurrency microbenchmark | Warm reuse avoided new simulated service calls; eight concurrent duplicates coalesced to one call; parallel search preserved result order | [io-benchmark.json](io-benchmark.json) |

Replay answers use scripted model outputs and local lexical retrieval. Content-presence checks and required-evidence recall are regression proxies; they do not measure legal correctness or expert answer quality. The combined replay made 24 scripted generation operations versus baseline 18, including additional verification of previously unaudited answers. Zero recorded replay charges mean no paid provider calls occurred, not that production generation is free.

The I/O benchmark uses an artificial service delay and omits production model latency, SQL version reads and real traffic. It demonstrates mechanisms working; it does not establish a production percentage improvement. Additional verification and cache misses can increase individual request cost or latency.

## Activation status

| Area | Current state | Required before promotion |
|---|---|---|
| Lean history access and Lab scope correction | Implemented as immediate fixes | Review normal deployment checks |
| Context, evidence, planning, routing and verification policies | Legacy defaults retained; candidates independently selectable | Representative held-out quality comparison, including corrections, exceptions, dates and multilingual cases |
| Exact embedding/retrieval/answer caches | Off by default | Dedicated cache credentials, target version migrations, isolation/freshness review and observed reuse |
| Batched neighbors, concurrency and transport pooling | Off/default serial | Target helper migration as applicable; equivalent-ordering and load comparison |
| Extraction diagnostics and targeted repairs | Opt-in; pilot uses synthetic documents | Representative document checks before reprocessing any corpus |

Use [README.md](README.md) for migration order and staged rollout, [candidate.env](candidate.env) for evaluation settings and [rollback.env](rollback.env) to restore legacy policies and bypass caches. Do not enable all candidates merely because the local suite passes.

No live migrations, corpus reindex, deployment, paid evaluation or edits to the existing local `.env` were performed. Existing staged cost-workbook artifacts and unrelated reports were preserved. Changes remain uncommitted for review.
