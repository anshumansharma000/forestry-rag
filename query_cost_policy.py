"""Versioned task decisions. Scores are routing signals, not correctness probabilities."""

import math
import re
from dataclasses import dataclass

from chunking import count_tokens
from jev_settings import number
from temporal import temporal_metadata

ROUTING_VERSION = "jev-led-v1"
AUDIT_VERSION = "independent-v1"
CATEGORIES = {"bounded": "answer_direct", "complex": "answer_complex", "reconciliation": "answer_complex_high"}

# Require a relationship or applicability task, rather than a date or amendment keyword.
RECONCILIATION = re.compile(
    r"\b(?:which (?:rule|law|provision).{0,45}(?:prevails|governs|applies|in force)|"
    r"(?:does|do|is|are|would|can|should|may).{0,65}(?:apply to|applicable to|our case|my case)|"
    r"(?:resolve|reconcile|interpret).{0,50}(?:conflict|amendment|judgment|precedence)|"
    r"how.{0,50}(?:rules?|laws?|amendments?|judgments?).{0,35}affect.{0,20}(?:me|us|our|my)|"
    r"(?:whether|determine).{0,50}(?:applies?|applicable|in force)|"
    r"(?:conflicting|contradictory) (?:rules|provisions|laws)|"
    r"(?:was|is|remains?).{0,35}(?:legally applicable|in force|superseded|repealed))\b|"
    r"(?:कौन सा|कौनसा).{0,35}(?:लागू|प्रभावी)|(?:मेरे|हमारे).{0,25}मामले.{0,25}लागू|"
    r"(?:विरोधाभासी|परस्पर विरोधी).{0,25}(?:नियम|प्रावधान)",
    re.I,
)
JUDICIAL = re.compile(r"\b(?:interpret|meaning|implication|binding|precedent|ratio|held|holding)\b|व्याख्या|बाध्यकारी", re.I)
JUDICIAL_TASK = re.compile(r"\b(?:interpret|meaning of).{0,45}(?:court|judicial|judgment|holding)\b", re.I)
DRAFT_RISK = re.compile(
    r"\b(?:supersedes?|overrides?|prevails? over|takes? precedence|remains? in force|"
    r"(?:applies?|applicable) to (?:your|our|this|my|the present) (?:case|situation|project)|"
    r"currently (?:applies?|applicable)|(?:governing|applicable) law|"
    r"(?:remains?|is still) (?:legally )?applicable|"
    r"(?:court|judgment).{0,35}(?:held|binding|interpreted))\b|"
    r"(?:आपके|इस|हमारे) मामले.{0,20}लागू|अधिक्रमित|पूर्वता|न्यायालय.{0,30}(?:निर्णय दिया|बाध्यकारी)",
    re.I,
)


def safeguards(question, contexts, plan=None):
    """Narrow positive risk signals; unknown evidence is never proof of a conflict."""
    reasons = []
    if (plan or {}).get("conflicts"):
        reasons.append("plan_conflict")
    if RECONCILIATION.search(question):
        reasons.append("applicability_reconciliation")
    if JUDICIAL_TASK.search(question):
        reasons.append("judicial_interpretation")
    for row in contexts:
        metadata = temporal_metadata(row)
        review = metadata.get("legal_review") or {}
        if review.get("confirmed_conflict") is True:
            reasons.append("confirmed_evidence_conflict")
        if (metadata.get("legal_profile") or {}).get("instrument_type") == "judicial" and JUDICIAL.search(question):
            reasons.append("judicial_interpretation")
    return tuple(dict.fromkeys(reasons))


def bounded_work(question, contexts, plan=None):
    if not contexts or len(contexts) > 10 or safeguards(question, contexts, plan):
        return False
    if (plan or {}).get("unknowns"):
        return False
    from query_context import current_context

    context = current_context()
    if context is not None and context.unresolved:
        return False
    documents = {row.get("document_id") for row in contexts}
    if None in documents or len(documents) > 3:
        return False
    for row in contexts:
        metadata = temporal_metadata(row)
        review = metadata.get("legal_review") or {}
        profile = metadata.get("legal_profile") or {}
        if not (row.get("text") or "").strip() or (profile and not profile.get("reviewed")):
            return False
        if any(review.get(key) for key in ("missing_reviewed_evidence", "missing_relationship_count", "relationship_search_truncated")):
            return False
    return count_tokens("\n".join(row["text"] for row in contexts)) <= 6000


def confident_category(scores):
    if not isinstance(scores, dict) or any(
        type(scores.get(k)) not in (int, float) or not math.isfinite(scores[k]) or not 0 <= scores[k] <= 1 for k in CATEGORIES
    ):
        return None
    threshold = number("JEV_ROUTE_THRESHOLD", .80, .5, 1)
    margin = number("JEV_ROUTE_MARGIN", .20, .01, 1)
    ranked = sorted(CATEGORIES, key=scores.get, reverse=True)
    first, second = ranked[:2]
    return first if scores[first] >= threshold and scores[first] - scores[second] + 1e-12 >= margin else None


@dataclass(frozen=True)
class RouteDecision:
    operation: str
    owner: str
    reason: str
    safeguards: tuple[str, ...] = ()
    version: str = ROUTING_VERSION


def select_route(question, contexts, proposals, plan=None, *, active=False):
    guards = safeguards(question, contexts, plan)
    if guards:
        return RouteDecision("answer_complex_high", "safeguard", guards[0], guards)
    category = confident_category(proposals.get("scores")) if active else None
    if category:
        return RouteDecision(CATEGORIES[category], "jev", "confident_" + category)
    operation = "answer_direct" if bounded_work(question, contexts, plan) else "answer_complex"
    reason = proposals.get("routing_status", "routing_off") if active else "routing_off"
    return RouteDecision(operation, "fallback", reason)


def verification_reason(question, contexts, draft, plan=None, scores=None):
    guards = safeguards(question, contexts, plan)
    if guards:
        return guards[0]
    if confident_category(scores) == "reconciliation":
        return "task_reconciliation"
    if DRAFT_RISK.search(draft):
        return "draft_reconciliation_claim"
    return "bounded_support_audit"
