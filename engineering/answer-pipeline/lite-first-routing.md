# Lite-first routing — 16 October 2026

The requested model split remains Gemini 3.5 Flash Lite for routine work and Gemini 3.8 Flash for harder work. No provider model identifiers were changed.

`RAG_LITE_FIRST=true` is now the default and is explicit in deployment examples and Render configuration. Restart/redeploy the running application to load the updated code. Setting it to `false` restores the prior routing behavior. `RAG_COST_OPTIMIZATIONS=false` also disables the new policy. The existing local `.env` was not edited.

Previously, procedure/overview question shapes generally selected Flash; active Jev routing could additionally promote an otherwise routine question merely because it required combining excerpts. The new deterministic eligibility check permits routine fact lookups, procedures and summaries over at most ten passages, three identified documents and 6,000 source tokens. Ordinary inline exceptions remain in the evidence and answer audit. A redundant planning pass is omitted for these bounded tasks.

Flash remains selected for historical/current-law questions, applicability, judicial interpretation, conflicts and difficult comparisons. Unresolved conversation scope, unreviewed legal profiles, review gaps, amendment references, explicit cross-instrument conditions, oversized evidence and plan uncertainty do not qualify for the new Lite shortcut. Existing conservative amendment handling remains intact; reducing those promotions needs reliable provenance rather than ignoring amendment metadata.

Active Jev may still promote an eligible question when it detects legal risk. A synthesis-only proposal cannot promote a deterministically eligible routine answer. Every answer newly covered by the Lite shortcut receives the existing support audit, even if the general answer-verification switch is disabled. A failed audit retains the existing bounded stronger-verifier escalation and abstention behavior; a negative active Jev decision remains final.

`answer_route_selected` logs and request trace events include the effective Lite-first setting alongside the selected operation, question shape, context count and planning details. Actual model-usage records remain the authority for provider calls: some requests can use Lite for drafting and Flash for a failed or high-risk audit.

Validation: 457 ordinary tests passed, including 18 new routing/audit tests; 25 opt-in integration tests were skipped in this run. The previous complete integration run passed before this routing follow-up. No live model calls or production deployment were performed. The Lite proportion and net savings depend on the real question/evidence mix and have not been measured.

The older synthetic replay explicitly retains `RAG_LITE_FIRST=false` so its stored baseline/candidate comparison remains about the original priorities 1–6. It is not an evaluation of this follow-up policy.
