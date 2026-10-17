"""Deterministic evidence diagnostics. Coverage labels are not legal determinations."""

import re
from dataclasses import dataclass
from datetime import date

from chunking import count_tokens
from temporal import historical_question, parse_date, temporal_metadata

QUALIFICATION = re.compile(r"\b(?:except\w*|unless|provided|notwithstanding|exempt\w*|subject to|amend\w*|supersed\w*|repeal\w*)\b", re.I)
RISK = re.compile(r"\b(?:current\w*|latest|as of|in force|applicab\w*|court|judicial|conflict\w*|amend\w*|supersed\w*|repeal\w*)\b", re.I)


@dataclass(frozen=True)
class EvidenceRequirements:
    shape: str
    applicability: bool
    comparison: bool
    procedure: bool

    @classmethod
    def from_question(cls, question):
        from retrieval import classify_question_shape

        shape = classify_question_shape(question)
        return cls(shape, bool(RISK.search(question) or historical_question(question)), shape == "comparison", shape == "procedure")


def diagnostics(question, contexts):
    req = EvidenceRequirements.from_question(question)
    reviews = [(c.get("metadata") or {}).get("legal_review") or {} for c in contexts]
    missing = sum(
        bool(r.get("missing_reviewed_evidence") or r.get("missing_relationship_count") or r.get("relationship_search_truncated"))
        for r in reviews
    )
    terms = set(re.findall(r"[\w]+", question.lower())) - {"what", "is", "the", "a", "of", "for", "and", "how", "do", "i"}
    hits = sum(bool(terms & set(re.findall(r"[\w]+", c.get("text", "").lower()))) for c in contexts)
    return {
        "context_count": len(contexts),
        "matched_count": sum(c.get("evidence_role") == "matched" for c in contexts),
        "qualification_count": sum(bool(QUALIFICATION.search(c.get("text", ""))) for c in contexts),
        "review_gap_count": missing,
        "lexically_related_count": hits,
        "applicability_required": req.applicability,
        "evidence_tokens_estimate": sum(count_tokens(c.get("text", "")) for c in contexts),
        "sufficiency": "unverified",
    }


def coherent_synthesis(question, contexts):
    """Experimental shortcut restricted to small unconditional single-document evidence."""
    req = EvidenceRequirements.from_question(question)
    if req.applicability or req.comparison or req.shape not in ("procedure", "overview"):
        return False
    if re.search(r"\b(?:detail\w*|all|comprehensive|exhaustive|thorough)\b", question, re.I):
        return False
    if not 0 < len(contexts) <= 4 or len({c.get("document_id") for c in contexts}) != 1:
        return False
    if any(
        not c.get("document_id") or not c.get("text") or QUALIFICATION.search(c["text"]) or temporal_metadata(c).get("amendment_references")
        for c in contexts
    ):
        return False
    from prompts import answer_generation_operation

    return (
        not historical_question(question)
        and answer_generation_operation(question, contexts) != "answer_complex_high"
        and diagnostics(question, contexts)["review_gap_count"] == 0
        and sum(count_tokens(c["text"]) for c in contexts) <= 2000
    )


def material_amendment(context):
    metadata = temporal_metadata(context)
    if not metadata.get("amendment_references"):
        return False
    from pipeline_settings import PipelineSettings

    if PipelineSettings.read().route != "v2":
        return True
    profile = metadata.get("legal_profile") or {}
    # Only reviewed principal instruments permit ignoring inherited document-level
    # references when the actual selected provision contains no qualification.
    return not (
        profile.get("reviewed") is True
        and profile.get("instrument_type") in ("act", "rules")
        and (effective := parse_date(profile.get("effective_date"))) is not None
        and effective <= date.today()
        and not QUALIFICATION.search(context.get("text", ""))
    )


def prioritize(contexts):
    """Keep anchors; within optional neighbors protect material qualification text."""
    anchors = [c for c in contexts if c.get("evidence_role") != "neighbor"]
    neighbors = [c for c in contexts if c.get("evidence_role") == "neighbor"]
    return [*anchors, *sorted(neighbors, key=lambda c: bool(QUALIFICATION.search(c.get("text", ""))), reverse=True)]


def protect_qualifications(question, candidates):
    """Reserve a relevant qualifying match after the strongest anchor, never drop rows."""
    if len(candidates) < 2:
        return candidates
    from retrieval import meaningful_terms

    anchor = candidates[0]
    terms = meaningful_terms(question)
    threshold = max(0.0, float(anchor.get("score", 0)) * 0.75)
    protected = []
    protected_docs = set()
    for candidate in candidates[1:12]:
        doc = candidate.get("document_id")
        if (
            doc
            and doc == anchor.get("document_id")
            and doc not in protected_docs
            and candidate.get("score", 0) >= threshold
            and QUALIFICATION.search(candidate.get("text", ""))
            and terms & meaningful_terms(candidate.get("text", ""))
        ):
            protected.append(candidate)
            protected_docs.add(doc)
    identities = {id(c) for c in protected}
    return [anchor, *protected, *(c for c in candidates[1:] if id(c) not in identities)]


def unicode_terms(text, stop_words):
    """Keep letters, digits and combining marks together; preserve numeric dots."""
    import unicodedata

    normalized = unicodedata.normalize("NFC", text.casefold())
    tokens, current = [], []
    for i, char in enumerate(normalized):
        numeric_dot = char == "." and i > 0 and i + 1 < len(normalized) and normalized[i - 1].isdigit() and normalized[i + 1].isdigit()
        if unicodedata.category(char)[0] in ("L", "N", "M") or numeric_dot:
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    # Negation is deliberately retained. Source text itself is never altered.
    stop_words = stop_words | {"क्या", "है", "हैं", "का", "की", "के", "को", "से", "में", "और", "लिए", "एक"}
    return {t for t in tokens if len(t) > 1 and t not in stop_words}
