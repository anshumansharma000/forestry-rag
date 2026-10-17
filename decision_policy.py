"""Eligibility for optional decisions: no new classifier calls."""

from hashlib import sha256

from pipeline_settings import PipelineSettings
from pipeline_trace import event


def sample_shadow(feature: str, query_id: str | None) -> bool:
    rate = PipelineSettings.read().shadow_sample_rate
    fraction = int.from_bytes(sha256(f'{query_id or "unscoped"}:{feature}'.encode()).digest()[:8], "big") / 2**64
    selected = fraction < rate
    if rate != 1:
        event("shadow_sample", feature=feature, selected=selected, sample_rate=rate)
    return selected


def effective_questions(*, route: str, baseline_plan: bool, allowed: bool, removable_plan: bool):
    """Return whether either possible proposal could change the guarded result."""
    routing = route != "answer_complex_high"
    planning = allowed and (not baseline_plan or removable_plan)
    return routing, planning


def rerank_metadata(metadata):
    if not PipelineSettings.read().prune_decisions:
        return metadata
    fields = (
        "title",
        "authority",
        "issued_date",
        "effective_date",
        "temporal_metadata_version",
        "amendment_references",
        "identifiers",
        "legal_profile",
        "legal_review",
    )
    return {key: metadata[key] for key in fields if key in metadata}
