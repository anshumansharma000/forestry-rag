"""Conservative, deterministic eligibility for lower-cost source extraction."""

import re

from chunking import count_tokens
from temporal import historical_question, temporal_metadata


def lite_first_enabled() -> bool:
    from settings import env_bool

    return env_bool("RAG_LITE_FIRST", True) and env_bool("RAG_COST_OPTIMIZATIONS", True)


def routine_lite_eligible(question: str, contexts: list[dict], plan: dict | None = None) -> bool:
    """Bounded source synthesis is routine; legal reconciliation is not."""
    if not lite_first_enabled() or not 0 < len(contexts) <= 10:
        return False
    if plan and (plan.get("conflicts") or plan.get("unknowns")):
        return False
    if historical_question(question) or re.search(
        r"\b(?:current\w*|latest|applicab\w*|apply to|in force|as of|before|after|"
        r"conflict\w*|contradict\w*|supersed\w*|repeal\w*|amend\w*|precedence|"
        r"compare\w*|comparison|versus|differ\w*|can (?:i|we)|may (?:i|we)|"
        r"our case|my case|should|interpret\w*|court|judgment|judicial|"
        r"comprehensive|exhaustive|thorough|in.depth)\b",
        question,
        re.I,
    ):
        return False
    documents = {c.get("document_id") for c in contexts}
    if None in documents or len(documents) > 3:
        return False
    from query_context import current_context

    context = current_context()
    if context is not None and context.unresolved:
        return False
    for row in contexts:
        if not (row.get("text") or "").strip():
            return False
        metadata = temporal_metadata(row)
        profile = metadata.get("legal_profile") or {}
        review = metadata.get("legal_review") or {}
        if profile and (not profile.get("reviewed") or profile.get("instrument_type") == "judicial"):
            return False
        if any(review.get(k) for k in ("missing_reviewed_evidence", "missing_relationship_count", "relationship_search_truncated")):
            return False
        # Keep actual temporal/cross-instrument reconciliation on Flash, while
        # ordinary inline exceptions remain visible to Lite and its verifier.
        if metadata.get("amendment_references") or re.search(
            r"\b(?:amend\w*|supersed\w*|repeal\w*|notwithstanding|read with|in conjunction|"
            r"subject to (?:rule|section|article|regulation))\b",
            row["text"],
            re.I,
        ):
            return False
    return count_tokens("\n".join(c["text"] for c in contexts)) <= 6000


def extraction_eligible(question: str, contexts: list[dict], plan: dict | None = None) -> bool:
    if not contexts or len(contexts) > 4 or (plan and (plan.get("conflicts") or plan.get("unknowns"))):
        return False
    documents = {ctx.get("document_id") for ctx in contexts}
    if len(documents) != 1 or None in documents:
        return False
    if historical_question(question) or re.search(
        r"\b(?:current|currently|latest|applicab\w*|apply to|in force|as of|before|after|"
        r"conflict\w*|contradict\w*|supersed\w*|repeal\w*|precedence|which (?:rule|law)|"
        r"can (?:i|we)|may (?:i|we)|our case|my case|should|interpret\w*|court|judgment|judicial)\b",
        question,
        re.I,
    ):
        return False
    explicit_extraction = bool(
        re.search(
            r"\b(?:quote|extract|list|summari[sz]e|what documents|which documents|"
            r"what forms|which forms|how do i apply|how to apply)\b",
            question,
            re.I,
        )
    )
    if not explicit_extraction:
        return False
    texts = []
    for ctx in contexts:
        text = ctx.get("text") or ""
        if not text.strip():
            return False
        metadata = temporal_metadata(ctx)
        profile = metadata.get("legal_profile") or {}
        review = metadata.get("legal_review") or {}
        if profile and (not profile.get("reviewed") or profile.get("instrument_type") == "judicial"):
            return False
        if any(review.get(key) for key in ("missing_reviewed_evidence", "missing_relationship_count", "relationship_search_truncated")):
            return False
        # Temporal precedence and cross-instrument conditions retain the stronger model.
        if metadata.get("amendment_references") or re.search(
            r"\b(?:amend\w*|supersed\w*|repeal\w*|court|judgment|judicial|notwithstanding|subject to|"
            r"except\w*|exempt\w*|unless|provided|read with|in conjunction)\b",
            text,
            re.I,
        ):
            return False
        texts.append(text)
    return count_tokens("\n".join(texts)) <= 2000
