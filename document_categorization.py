"""Document-level suggestions and explicit corrections, independent of chunk profiles."""

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from temporal import DATE_TOKEN, extract_temporal_metadata, parse_date

DocumentCategory = Literal[
    "policy", "constitution", "act", "amendment", "rules", "consolidated_guidelines",
    "guidelines", "circular", "clarification", "notification", "judgment_order", "handbook", "other",
]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
CATEGORY_LABELS = {
    "policy": "Policy", "constitution": "Constitution", "act": "Act", "amendment": "Amendment",
    "rules": "Rules", "consolidated_guidelines": "Consolidated Guidelines", "guidelines": "Guidelines",
    "circular": "Circular", "clarification": "Clarification", "notification": "Notification",
    "judgment_order": "Judgment / Court Order", "handbook": "Handbook / Compilation", "other": "Other / Unclassified",
}


class CategorizationInput(BaseModel):
    """Partial correction. Explicit null clears a suggestion; omitted fields retain it."""

    model_config = ConfigDict(extra="forbid")
    document_type: DocumentCategory | None = None
    subjects: list[Text] | None = Field(default=None, max_length=30)
    issuing_authority: Text | None = None
    jurisdiction: Text | None = None
    state: Text | None = None
    issued_date: date | None = None
    effective_date: date | None = None
    consolidation_as_of: date | None = None
    identifiers: list[Text] | None = Field(default=None, max_length=100)


def _suggested_dates(text: str) -> dict:
    # The legacy extractor accepts the generic label "Date". Prevent other
    # explicitly named date fields from being mistaken for the issue date.
    normalized = re.sub(r"\beffective date[ \t]*[:,-]?[ \t]*", "Effective from ", text, flags=re.I)
    normalized = "\n".join(
        line for line in normalized.splitlines()
        if not re.search(r"\b(?:commencement|consolidation|publication|hearing|expiry|expiration|reference) date\b", line, re.I)
    )
    return extract_temporal_metadata(normalized)


def infer_categorization(source: str, title: str, pages: list[dict]) -> dict:
    """Conservative, source-backed suggestions. Never infer document relationships."""
    from documents import extract_legal_identifiers, infer_authority

    evidence = {}
    values = {}
    # Prefer the title/cover over citations appearing later in the document.
    patterns = (
        ("constitution", r"\bconstitution of india\b"),
        ("amendment", r"\bamend(?:ment|ments|ing)\b"),
        ("consolidated_guidelines", r"\bconsolidated guidelines?\b"),
        ("policy", r"\bpolicy\b"),
        ("judgment_order", r"\b(?:judgment|judgement|supreme court|high court)\b"),
        ("clarification", r"\bclarifications?\b"),
        ("circular", r"\bcirculars?\b"),
        ("notification", r"\bnotification\b"),
        ("handbook", r"\b(?:handbook|compilation|conservancy)\b"),
        ("guidelines", r"\bguidelines?\b"),
        ("rules", r"\brules\b"),
        ("act", r"\bact\b"),
    )
    candidates = [(None, f"{title}\n{source}")]
    if pages:
        candidates.append((pages[0].get("page"), pages[0].get("text", "")[:3000]))
    for page, text in candidates:
        match = next(((kind, re.search(pattern, text, re.I)) for kind, pattern in patterns if re.search(pattern, text, re.I)), None)
        if match:
            values["document_type"] = match[0]
            evidence["document_type"] = [{"page": page, "text": text[max(0, match[1].start() - 80):match[1].end() + 120]}]
            break

    # Inspect every extracted page. Conflicting labeled dates remain unknown.
    combined = "\n".join(p.get("text", "") for p in pages)
    subjects = []
    subject_evidence = []
    for label, pattern in (
        ("Forest conservation / FCA", r"\bforest[\s(]+conservation\b|\bFCA\b"),
        ("Forest conservancy", r"\bforest conservancy\b"),
        ("National Forest Policy", r"\bnational forest policy\b"),
        ("Constitution of India", r"\bconstitution of india\b"),
    ):
        match = re.search(pattern, f"{title}\n{source}", re.I)
        if match:
            subjects.append(label)
            subject_evidence.append({"page": None, "text": match.group(0)})
    if subjects:
        values["subjects"] = subjects
        evidence["subjects"] = subject_evidence
    cutoff_hits = [
        (p.get("page"), m, parse_date(m.group(1))) for p in pages
        for m in re.finditer(rf"\b(?:consolidated as (?:of|on)|updated up to)[ \t:,-]*({DATE_TOKEN})", p.get("text", ""), re.I)
    ]
    cutoffs = {d.isoformat() for _, _, d in cutoff_hits if d is not None}
    if len(cutoffs) == 1:
        values["consolidation_as_of"] = cutoffs.pop()
        evidence["consolidation_as_of"] = [{"page": p, "text": m.group(0)} for p, m, _ in cutoff_hits[:5]]
    dates = _suggested_dates(combined)
    for field in ("issued_date", "effective_date"):
        if field in dates:
            values[field] = dates[field]
            evidence[field] = [
                {"page": p.get("page"), "text": line.strip()[:500]}
                for p in pages for line in p.get("text", "").splitlines()
                if _suggested_dates(line).get(field) == dates[field]
            ][:5]
    authority_candidates = {}
    for p in pages[:2]:
        authority = infer_authority(p.get("text", ""))
        if authority:
            authority_candidates.setdefault(authority, []).append({"page": p.get("page"), "text": authority})
    if len(authority_candidates) == 1:
        authority = next(iter(authority_candidates))
        values["issuing_authority"] = authority
        evidence["issuing_authority"] = authority_candidates[authority]
    identifiers = extract_legal_identifiers(combined)
    if identifiers:
        values["identifiers"] = identifiers
        evidence["identifiers"] = [
            {"page": p.get("page"), "text": line.strip()[:500]}
            for p in pages for line in p.get("text", "").splitlines() if extract_legal_identifiers(line)
        ][:20]
    # Jurisdiction is extracted only from explicit labels, never place mentions.
    for field, pattern in (
        ("jurisdiction", r"^[ \t]*Jurisdiction[ \t]*:[ \t]*(.{1,200})$"),
        ("state", r"^[ \t]*State[ \t]*:[ \t]*(.{1,200})$"),
    ):
        hits = [(p.get("page"), m) for p in pages for m in re.finditer(pattern, p.get("text", ""), re.I | re.M)]
        unique = {m.group(1).strip() for _, m in hits}
        if len(unique) == 1:
            values[field] = unique.pop()
            evidence[field] = [{"page": page, "text": m.group(0).strip()} for page, m in hits[:5]]
    return merge_categorization({"values": values, "evidence": evidence}, {})


def merge_categorization(suggestions: dict, existing: dict) -> dict:
    overrides = existing.get("overrides") or {}
    values = {**(suggestions.get("values") or {}), **overrides}
    return {
        "version": 1,
        "values": values,
        "overrides": overrides,
        "field_sources": {key: "manual" if key in overrides else "inferred" for key in values},
        "evidence": suggestions.get("evidence") or {},
        "review_required": not values.get("document_type") or any(key not in overrides for key in values),
        "missing_fields": [key for key in CategorizationInput.model_fields if not values.get(key)],
        **{key: existing[key] for key in ("registry_namespace", "updated_by") if key in existing},
    }
