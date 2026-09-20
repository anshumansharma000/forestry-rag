"""Conservative, deterministic eligibility for lower-cost source extraction."""
import re

from chunking import count_tokens
from temporal import historical_question, temporal_metadata


def extraction_eligible(question: str, contexts: list[dict], plan: dict | None = None) -> bool:
    if not contexts or len(contexts) > 4 or (plan and (plan.get('conflicts') or plan.get('unknowns'))):
        return False
    documents = {ctx.get('document_id') for ctx in contexts}
    if len(documents) != 1 or None in documents:
        return False
    if historical_question(question) or re.search(
        r'\b(?:current|currently|latest|applicab\w*|apply to|in force|as of|before|after|'
        r'conflict\w*|contradict\w*|supersed\w*|repeal\w*|precedence|which (?:rule|law)|'
        r'can (?:i|we)|may (?:i|we)|our case|my case|should|interpret\w*|court|judgment|judicial)\b', question, re.I,
    ):
        return False
    explicit_extraction = bool(re.search(r'\b(?:quote|extract|list|summari[sz]e|what documents|which documents|'
                                         r'what forms|which forms|how do i apply|how to apply)\b', question, re.I))
    if not explicit_extraction:
        return False
    texts = []
    for ctx in contexts:
        text = ctx.get('text') or ''
        if not text.strip():
            return False
        metadata = temporal_metadata(ctx)
        profile = metadata.get('legal_profile') or {}
        review = metadata.get('legal_review') or {}
        if profile and (not profile.get('reviewed') or profile.get('instrument_type') == 'judicial'):
            return False
        if any(review.get(key) for key in ('missing_reviewed_evidence', 'missing_relationship_count',
                                           'relationship_search_truncated')):
            return False
        # Temporal precedence and cross-instrument conditions retain the stronger model.
        if metadata.get('amendment_references') or re.search(
            r'\b(?:amend\w*|supersed\w*|repeal\w*|court|judgment|judicial|notwithstanding|subject to|'
            r'except\w*|exempt\w*|unless|provided|read with|in conjunction)\b', text, re.I,
        ):
            return False
        texts.append(text)
    return count_tokens('\n'.join(texts)) <= 2000
