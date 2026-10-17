# Implementation and offline validation — 17 October 2026

Implemented the requested opt-in routing, audit and evidence policies, complete API cost
accounting, sanitized log fixtures and a reproducible offline comparison. Existing local
work was preserved. No paid calls, live data writes, deployment, migration or `.env` edit
was performed. The new behavior controls default to legacy until separately activated.

Validation completed:

- Initial ordinary suite: 461 passed, 25 opt-in integration tests skipped.
- Final ordinary suite: **518 passed, 25 skipped**. The only warning is the existing
  Supabase `gotrue` deprecation warning.
- Lint passed for all Python files changed in this implementation.
- `git diff --check` passed.
- Offline evaluator completed and regenerated [offline-report.json](offline-report.json).
  Its provider transports are blocked; it has no live mode.

The 57 new acceptance cases cover category confidence/margins, downgrades/promotions,
multilingual and historical lookups, narrow conflict/applicability/judicial safeguards,
uncertainty, unavailable/oversized/malformed Jev responses, one combined decision call,
late plan conflicts, off/shadow behavior, shared conversation scope, audit independence,
the previously unaudited factual path, negative Jev finality, bounded escalation,
technical failures, exact quotes, uncited exceptions, provenance/page/citation retention,
number/word-prefix containment safety, plan compaction, cache isolation, separate strong
audit settings, missing usage, future rates, retry and structured-regeneration accounting,
all-API targets and historical log reconciliation.

All five answered historical subtotals reconcile exactly within floating-point tolerance,
without summing receipts and summaries twice. Historical embedding totals remain unknown.
The interrupted request retains INR 0.5865894 in known receipts without a completion
summary. Scripted baseline/candidate cases demonstrate different call paths and token
estimates; their costs are simulations and do not prove live savings or answer accuracy.

Use [candidate.env](candidate.env), [rollback.env](rollback.env) and [README.md](README.md)
for activation, compatibility and remaining evaluation requirements. Production promotion
still needs original query/evidence snapshots, reviewed quality labels and a separately
authorized paid evaluation. No INR 1.50 ceiling or live quality parity is claimed.
