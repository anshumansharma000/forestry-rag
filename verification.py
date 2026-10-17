"""Explicit v2 audit schema and mechanical checks; no implicit approval retries."""

import re
from dataclasses import dataclass

ISSUE_CODES = (
    "citation_defect",
    "unsupported_claim",
    "missing_condition",
    "scope_mismatch",
    "applicability_conflict",
    "insufficient_evidence",
    "invalid_audit_output",
    "technical_failure",
)


@dataclass(frozen=True)
class VerificationDecision:
    owner: str
    reason: str
    required: bool = True
    version: str = "legacy"


def audit_schema(base):
    return {
        **base,
        "properties": {**base["properties"], "issue_codes": {"type": "array", "items": {"type": "string", "enum": list(ISSUE_CODES)}}},
        "required": [*base["required"], "issue_codes"],
    }


def valid_audit(result):
    if not isinstance(result, dict) or type(result.get("supported")) is not bool:
        return False
    if not isinstance(result.get("issues"), list) or any(not isinstance(x, str) for x in result["issues"]):
        return False
    codes = result.get("issue_codes", [])  # Accept legacy audits during rolling upgrade.
    return isinstance(codes, list) and all(x in ISSUE_CODES for x in codes)


def quotes_match(answer, excerpts):
    """Check explicit quotation spans only; this never establishes applicability."""
    quotations = re.findall(r'“([^”]+)”|"([^"\n]+)"', answer)
    return all((left or right) in excerpts for left, right in quotations)
