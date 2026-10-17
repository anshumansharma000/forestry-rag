"""Lossless prompt compaction with stable, one-based public citation identities."""

import json
import unicodedata
from dataclasses import dataclass

from chunking import count_tokens
from pipeline_trace import event
from temporal import temporal_metadata


def literal_span(text, container):
    """Never treat a number/word prefix as a contained legal assertion."""
    if not text:
        return None

    def word(char):
        return char == "_" or unicodedata.category(char)[0] in "LNM"

    start = container.find(text)
    while start >= 0:
        end = start + len(text)
        left_ok = not (start and word(text[0]) and word(container[start - 1]))
        right_ok = not (end < len(container) and word(text[-1]) and word(container[end]))
        # A numeral immediately followed by a decimal/fraction continuation differs.
        if text[-1].isdigit() and end + 1 < len(container) and container[end] in ".,/-" and container[end + 1].isdigit():
            right_ok = False
        if left_ok and right_ok:
            return start, end
        start = container.find(text, start + 1)
    return None


def provenance(row):
    # Keep unknown metadata conservatively; retrieval rank diagnostics are not provenance.
    metadata = {key: value for key, value in temporal_metadata(row).items() if key != "retrieval_fusion"}
    return (row.get("document_id"), row.get("source"), row.get("section_heading"),
            json.dumps(metadata, sort_keys=True, ensure_ascii=False, default=str),
            row.get("index_version"), row.get("revision_id"), row.get("version"), row.get("applicability"),
            row.get("jurisdiction"))


@dataclass(frozen=True)
class EvidenceBundle:
    serialized: str
    aliases: tuple[int, ...]
    before_tokens: int
    after_tokens: int

    @classmethod
    def build(cls, contexts, legacy, format_source):
        from legal_retrieval import legal_prompt_context

        blocks, aliases = [], []
        identities = [provenance(row) for row in contexts]
        for i, row in enumerate(contexts, 1):
            identity = identities[i - 1]
            text = row.get("text", "")
            # Longest containing passage is the representative; keep all original labels.
            candidates = [j for j, other in enumerate(contexts) if row.get("document_id")
                          and identity == identities[j] and text and literal_span(text, other.get("text", "")) is not None]
            representative = min(candidates, key=lambda j: (-len(contexts[j]["text"]), j)) + 1 if candidates else i
            aliases.append(representative)
            if representative != i:
                start, end = literal_span(text, contexts[representative - 1]["text"])
                blocks.append(f"[{i}] Source: {format_source(row)}; Section: {row.get('section_heading') or 'Not specified'}. "
                              f"Dates/legal annotation: same as [{representative}]. "
                              f"Evidence role: {row.get('evidence_role', 'matched')}. "
                              f"This excerpt is contained verbatim at characters {start}:{end} of [{representative}] (zero-based). "
                              f"Read the full text for qualifications; cite [{representative}] for claims outside this span.")
                continue
            metadata = temporal_metadata(row)
            annotation = ""
            profile = metadata.get("legal_profile")
            if profile:
                annotation = "Legal annotation (not excerpt evidence): " + json.dumps(profile, sort_keys=True) + "\n"
            blocks.append(f"[{i}] Source: {format_source(row)}\nSection: {row.get('section_heading') or 'Not specified'}; "
                          f"Evidence role: {row.get('evidence_role', 'matched')}\n"
                          f"Issue date: {metadata.get('issued_date') or 'Unknown'}; "
                          f"Effective date: {metadata.get('effective_date') or 'Unknown'}\n" + annotation + text)
        candidate = legal_prompt_context(contexts) + "\n\n".join(blocks)
        before, after = count_tokens(legacy), count_tokens(candidate)
        if after >= before:
            candidate, after, aliases = legacy, before, list(range(1, len(contexts) + 1))
        event("evidence_compaction", version="exact-v1", before_tokens=before, after_tokens=after,
              aliases=sum(i != ref for i, ref in enumerate(aliases, 1)))
        return cls(candidate, tuple(aliases), before, after)


def compact_plan(plan, contexts):
    """Replace only long, literal evidence copies; retain every generated conclusion."""
    def visit(value):
        if isinstance(value, str) and len(value) >= 80:
            refs = [str(i) for i, row in enumerate(contexts, 1) if literal_span(value, row.get("text", "")) is not None]
            if refs:
                return "Verbatim evidence: see excerpt [" + refs[0] + "]."
        if isinstance(value, dict):
            return {key: visit(item) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    before = json.dumps(plan, ensure_ascii=False)
    after = json.dumps(visit(plan), ensure_ascii=False)
    result = after if count_tokens(after) < count_tokens(before) else before
    event("plan_compaction", before_tokens=count_tokens(before), after_tokens=count_tokens(result))
    return result
