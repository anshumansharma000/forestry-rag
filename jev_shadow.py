"""Jev proposals with independently gated active application and shadow comparison."""
from hashlib import sha256

from jev_policy import BOUNDARY, decision_log, enabled
from model_routing import extraction_eligible
from services.jev import evaluate
from token_usage import track_query_usage


def ask(state: dict, checks: dict, operation: str) -> dict | None:
    return evaluate(state, {key: {"type": "noul", "instructions": BOUNDARY + instruction}
                            for key, instruction in checks.items()}, operation=operation)


def answer_policy(question: str, source_block: str, history_block: str, *, baseline_route: str,
                  baseline_planning: bool) -> dict:
    routing = enabled("routing") != "off"
    planning = enabled("planning") != "off"
    if not routing and not planning:
        return {}
    proposals = {}
    checks = {}
    if routing:
        checks.update({
            "extraction": "Can question be answered by direct extraction of explicit facts from excerpts without reconciling provisions?",
            "synthesis": "Does answering question require combining facts or steps across multiple excerpts?",
            "legal_risk": "Does answering question require resolving applicability, dates, conflicting provisions or legal precedence?",
        })
    if planning:
        checks["planning"] = (
            "Would a separate evidence-organizing plan materially help answer question because its requested scope "
            "requires several distinct themes, comparison, or reconciliation of evidence? Judge using excerpts and history."
        )
    # Do not show existing routing decisions or the generated plan to the evaluator.
    result = ask({"question": question, "excerpts": source_block, "history": history_block}, checks, "jev_answer_policy")
    if routing:
        proposal = "uncertain"
        if result:
            if result["legal_risk"] >= .8:
                proposal = "answer_complex_high"
            elif result["extraction"] >= .95 and result["legal_risk"] <= .05 and result["synthesis"] <= .1:
                proposal = "answer_direct"
            elif result["synthesis"] >= .8 and result["legal_risk"] <= .1:
                proposal = "answer_complex"
        proposals["route"] = proposal
        decision_log("routing", enabled("routing"), proposal != "uncertain", baseline_route=baseline_route,
                     proposed_route=proposal, probabilities={k: result[k] for k in ("extraction", "synthesis", "legal_risk")}
                     if result else None, disagreement=proposal != baseline_route if proposal != "uncertain" else None)
    if planning:
        probability = result["planning"] if result else None
        proposal = None if probability is None or .05 < probability < .95 else probability >= .95
        proposals["planning"] = proposal
        decision_log("planning", enabled("planning"), proposal is not None, baseline_planning=baseline_planning,
                     proposed_planning=proposal, probability=probability,
                     disagreement=proposal != baseline_planning if proposal is not None else None)
    return proposals


def apply_planning(proposals: dict, baseline: bool, question: str, contexts: list[dict], *,
                   baseline_route: str, history: bool, allowed: bool) -> bool:
    applied = baseline
    if enabled("planning") == "active" and allowed:
        if proposals.get("planning") is True:
            applied = True
        elif proposals.get("planning") is False and not history and baseline_route != "answer_complex_high":
            if extraction_eligible(question, contexts):
                applied = False
    if enabled("planning") != "off":
        decision_log("planning_applied", enabled("planning"), applied != baseline,
                     baseline_planning=baseline, applied_planning=applied)
    return applied


def apply_route(proposals: dict, baseline: str, question: str, contexts: list[dict], *,
                evidence_plan: dict | None, history: bool) -> str:
    applied = baseline
    if enabled("routing") == "active" and baseline != "answer_complex_high":
        proposed = proposals.get("route")
        if proposed == "answer_complex_high":
            applied = proposed
        elif proposed == "answer_complex" and baseline == "answer_direct":
            applied = proposed
        elif proposed == "answer_direct" and not history and extraction_eligible(question, contexts, evidence_plan):
            applied = proposed
    if enabled("routing") != "off":
        decision_log("routing_applied", enabled("routing"), applied != baseline,
                     baseline_route=baseline, applied_route=applied)
    return applied


def history_selection(messages: list[dict], latest: str, selected: list[dict]) -> list[dict]:
    rollout = enabled("history")
    if rollout == "off":
        return selected
    recent = messages[-10:]
    assistants = [i for i, m in enumerate(recent) if m["role"] == "assistant"]
    if len(assistants) < 2:
        return selected
    # Only evaluate long windows where reducing history can materially help.
    from chunking import count_tokens
    from settings import env_int

    if sum(count_tokens(m["content"]) for m in recent) <= env_int("RAG_HISTORY_SELECTION_TOKENS", 1500):
        return selected
    checks = {f"keep_{i}": f"Is history[{i}] needed to interpret latest or preserve a referenced quote, constraint, "
              "correction, unresolved ambiguity or numbered item? Only clearly unrelated older assistant replies may be omitted."
              for i in assistants[:-1]}
    result = ask({"latest": latest, "history": [{"role": m["role"], "content": m["content"]} for m in recent]},
                 checks, "jev_history")
    baseline = [i for i, m in enumerate(recent) if any(m is s for s in selected)]
    proposal = [i for i in range(len(recent)) if f"keep_{i}" not in checks or not result or result[f"keep_{i}"] > .01]
    decision_log("history", rollout, result is not None, baseline_indices=baseline,
                 proposed_indices=proposal if result else None, probabilities=result,
                 disagreement=proposal != baseline if result else None,
                 proposed_removed_tokens=sum(count_tokens(recent[i]["content"]) for i in range(len(recent))
                                             if i not in proposal) if result else None)
    if rollout == "active" and result:
        # Only remove clearly irrelevant replies from the baseline. Always restore
        # protected user/latest messages if a future selector omitted them.
        keep = set(baseline) & set(proposal)
        keep.update(i for i, m in enumerate(recent) if m["role"] != "assistant")
        keep.add(assistants[-1])
        return [m for i, m in enumerate(recent) if i in keep]
    return selected


def confident_label(result: dict, prefix: str, labels: tuple[str, ...]) -> str:
    ordered = sorted(labels, key=lambda label: result[f"{prefix}_{label}"], reverse=True)
    first, second = (result[f"{prefix}_{label}"] for label in ordered[:2])
    return ordered[0] if first >= .8 and first - second >= .2 else "uncertain"


def document_policy(doc: dict, *, baseline_profile: str | None, document_ref: str, origin: str) -> dict:
    classification = enabled("classification") != "off"
    extraction = enabled("extraction") != "off"
    if not classification and not extraction:
        return {}
    return _document_policy(doc, baseline_profile=baseline_profile, document_ref=document_ref, origin=origin,
                            classification=classification, extraction=extraction)


@track_query_usage
def _document_policy(doc: dict, *, baseline_profile: str | None, document_ref: str, origin: str,
                     classification: bool, extraction: bool) -> dict:
    from chunking import document_profile

    automatic_profile = baseline_profile in (None, "auto")
    if automatic_profile:
        baseline_profile = document_profile(doc)
    pages = doc.get("pages") or []
    # Deliberately sampled, not a completeness audit. No source document is mutated.
    indices = sorted({0, len(pages) // 2, len(pages) - 1}) if pages else []
    sampled = [{"page": pages[i].get("page"), "text": pages[i].get("text", "")[:4000],
                "truncated": len(pages[i].get("text", "")) > 4000,
                "method": pages[i].get("extraction_method", "unknown")} for i in indices]
    checks = {}
    profiles = ("faq", "procedure", "section")
    kinds = ("handbook", "act", "rules", "amendment", "guideline", "notification", "clarification", "instruction", "judicial")
    if classification:
        checks.update({f"profile_{label}": f"Is {label} the most suitable chunking profile for the sampled document? "
                       "faq means question-answer pairs; procedure means ordered steps; section means general section/clause text."
                       for label in profiles})
        checks.update({f"kind_{label}": f"Is the document itself a {label} instrument, rather than merely referring to one? "
                       "Use only the title and sampled text; unknown is appropriate when evidence is insufficient."
                       for label in kinds})
    if extraction:
        checks.update({f"quality_{i}": f"Does pages[{i}].text show obvious extraction corruption, garbled words or broken "
                       "table/reading order that warrants checking the original page? Valid Hindi, English, mixed languages, "
                       "and short text are not corruption. You cannot see the original, so do not assert missing unseen text."
                       for i in range(len(sampled))})
    if not checks:
        return {}
    result = ask({"title": doc.get("title"), "pages": sampled, "sampled_document": True}, checks, "jev_document_policy")
    reference = sha256(str(document_ref).encode()).hexdigest()[:20]
    common = {"document_ref": reference, "origin": origin, "sampled_pages": [p["page"] for p in sampled],
              "sample_count": len(sampled), "total_extracted_pages": len(pages)}
    applied = {}
    if classification:
        profile = confident_label(result, "profile", profiles) if result else "uncertain"
        kind = confident_label(result, "kind", kinds) if result else "uncertain"
        if enabled("classification") == "active" and automatic_profile and profile != "uncertain":
            applied["profile"] = profile
        if enabled("classification") == "active" and kind != "uncertain":
            # Store a non-authoritative suggestion for review. Legal hierarchy still
            # requires the existing reviewed profile before it can affect answers.
            applied["instrument_type_suggestion"] = kind
        decision_log("classification", enabled("classification"), result is not None, baseline_profile=baseline_profile,
                     baseline_document_type=(doc.get("metadata") or {}).get("document_type", "unknown"),
                     proposed_profile=profile, proposed_instrument_type=kind,
                     profile_disagreement=profile != baseline_profile if profile != "uncertain" else None,
                     applied_profile=applied.get("profile", baseline_profile),
                     probabilities={k: v for k, v in result.items() if not k.startswith("quality_")} if result else None, **common)
    if extraction:
        probabilities = {k: v for k, v in result.items() if k.startswith("quality_")} if result else None
        flagged = [sampled[i]["page"] for i in range(len(sampled)) if result and result[f"quality_{i}"] >= .8]
        blocked = enabled("extraction") == "active" and result is not None and any(p >= .99 for p in probabilities.values())
        applied["review_required"] = blocked
        if enabled("extraction") == "active" and flagged:
            applied["extraction_review_pages"] = flagged
        decision_log("extraction", enabled("extraction"), result is not None, proposed_review_pages=flagged if result else None,
                     review_required=blocked,
                     probabilities=probabilities, **common)
    return applied
