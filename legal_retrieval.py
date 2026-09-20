"""Opt-in handbook-led retrieval with explicit, bounded verification coverage."""
from datetime import date

from documents import extract_legal_identifiers
from legal_registry import STAGES, LegalRegistryRepository
from retrieval import (
    candidate_strength,
    context_from_row,
    embed_query,
    meaningful_terms,
    min_context_score,
    pack_contexts,
    rerank_candidates,
    retrieval_plan,
)
from temporal import historical_question

FACETS = {
    'handbook': 'handbook consolidated guidelines primer',
    'act': 'Act Adhiniyam governing section statutory provision',
    'rules': 'Rules procedure conditions exceptions',
    'amendment': 'amendment amended superseded repeal commencement',
    'implementation': 'guidelines clarification notification instruction enabling provision',
    'judicial': 'court judgment interim order stay subsequent modification Godavarman',
}


def related_documents(seeds: set[str], profiles: dict[str, dict], limit: int = 30) -> tuple[list[str], bool]:
    """Traverse both directions: an amendment points to its principal instrument."""
    edges = []
    for source, profile in profiles.items():
        if not profile.get('reviewed'):
            continue
        for link in profile.get('relationships', []):
            edges.append((source, str(link['target_document_id'])))
    seen = set(seeds)
    result = []
    for _ in range(3):
        found = set()
        for source, target in edges:
            if source in seen:
                found.add(target)
            if target in seen:
                found.add(source)
        new = sorted(found - seen)
        if not new:
            return result, False
        if len(result) + len(new) > limit:
            return result + new[:limit - len(result)], True
        result.extend(new)
        seen.update(new)
    more = any((source in seen) != (target in seen) for source, target in edges)
    return result, more


def retrieve_legal(question, top_k=None, repository=None, options=None, registry=None):
    from repositories import DocumentRepository

    repository = repository or DocumentRepository()
    registry = registry or LegalRegistryRepository()
    options = dict(options or {})
    if top_k is None and options.get('top_k') is None:
        options.setdefault('context_count', 10)
        options.setdefault('context_token_budget', 8000)
    plan = retrieval_plan(question, options, top_k)
    profiles = registry.profiles()  # Fail visibly if enabled without the migration; never claim a check succeeded.
    rows = {}
    stages = {}
    identifiers = extract_legal_identifiers(question)
    for stage, kinds in STAGES.items():
        query = f"{question} {' '.join(identifiers[:12])} {FACETS[stage]}"
        embedding = embed_query(query)
        stage_rows = registry.match(embedding, query, kinds, min(plan.candidate_count, 30))
        # Always search legacy/unclassified documents as well, including with no handbook hit.
        legacy_rows = repository.match_chunks(embedding, query, min(plan.candidate_count, 30))
        for row in [*stage_rows, *legacy_rows]:
            row_id = row['id']
            rows.setdefault(row_id, row)
            stages.setdefault(row_id, set()).add(stage)
        if stage == 'handbook':
            # Excerpt references guide subsequent searches; they are not verified relationships.
            for row in stage_rows[:3]:
                identifiers.extend(extract_legal_identifiers(row['content']))
            identifiers = list(dict.fromkeys(identifiers))[:12]

    seeds = {str(row['document_id']) for row in rows.values()}
    linked, truncated = related_documents(seeds, profiles)
    if linked:
        embedding = embed_query(question)
        for document_id in linked:
            for row in registry.match(embedding, question, [], 3, [document_id]):
                rows.setdefault(row['id'], row)
                stages.setdefault(row['id'], set()).add('relationship')

    contexts = []
    for rank, row in enumerate(rows.values()):
        context = context_from_row(row, rank)
        profile = profiles.get(str(row['document_id']), {})
        context['metadata'] = {**context['metadata'], 'legal_profile': profile,
                               'legal_search_stages': sorted(stages[row['id']])}
        if profile.get('reviewed'):
            for field in ('issued_date', 'effective_date'):
                if profile.get(field):
                    context['metadata'][field] = profile[field]
        contexts.append(context)
    ranking_question = question + (f" as of {options['as_of']}" if options.get('as_of') else '')
    ranked = rerank_candidates(ranking_question, contexts)
    # Drop clearly unrelated stage hits instead of treating a category match as legal evidence.
    query_terms = meaningful_terms(question)
    ranked = [ctx for ctx in ranked
              if candidate_strength(ctx) >= float(options.get('min_context_score', min_context_score()))
              and query_terms & meaningful_terms(ctx['text'] + ' ' + (ctx['section_heading'] or ''))]
    ordered = []
    # Protect controlling evidence against a high-scoring handbook crowding it out.
    for stage in ('judicial', 'act', 'rules', 'amendment', 'implementation', 'handbook'):
        candidate = next((ctx for ctx in ranked if
                          ctx['metadata']['legal_profile'].get('reviewed') and
                          ctx['metadata']['legal_profile'].get('instrument_type') in STAGES[stage]), None)
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    ordered.extend(ctx for ctx in ranked if ctx not in ordered)
    selected = pack_contexts(ordered, plan.context_count, plan.context_token_budget)
    present = {
        stage for stage, kinds in STAGES.items()
        if any(ctx['metadata']['legal_profile'].get('reviewed') and
               ctx['metadata']['legal_profile'].get('instrument_type') in kinds for ctx in selected)
    }
    selected_documents = {str(ctx['document_id']) for ctx in selected}
    missing_links = []
    for source, profile in profiles.items():
        if not profile.get('reviewed'):
            continue
        for link in profile.get('relationships', []):
            target = str(link['target_document_id'])
            if (source in selected_documents) != (target in selected_documents):
                missing_links.append({'source_document_id': source, **link})
    review = {
        'searched_stages': list(STAGES),
        'missing_reviewed_evidence': sorted(set(STAGES) - present),
        'relationship_search_truncated': truncated,
        'relationships_missing_selected_evidence': missing_links[:50],
        'missing_relationship_count': len(missing_links),
        'as_of': options.get('as_of') or ('historical date specified in question; verify from evidence'
                                        if historical_question(question) else date.today().isoformat()),
        'jurisdiction': options.get('jurisdiction'),
        'collection_currentness': 'unverified',
        'unclassified_evidence_count': sum(not ctx['metadata']['legal_profile'].get('reviewed') for ctx in selected),
        'selection_omitted_candidates': len(ranked) - len(selected),
        'scope': 'Uploaded documents only; search does not establish completeness or absence of later instruments.',
    }
    for context in selected:
        context['metadata']['legal_review'] = review
    return selected


def legal_prompt_context(contexts: list[dict]) -> str:
    import json

    review = next((ctx.get('metadata', {}).get('legal_review') for ctx in contexts
                   if ctx.get('metadata', {}).get('legal_review')), None)
    if not review:
        return ''
    return ('Legal verification requirements: Search order is not authority. Use the handbook for orientation; '
            'ground legal conclusions in applicable primary provisions. Amendments change only identified provisions. '
            'A Gazette publication has no automatic rank. Apply court orders according to scope, jurisdiction and '
            'operative status; interim orders are not final judgments. Check subsequent orders. Dates alone do not '
            'prove supersession. Reviewed links guide verification but do not substitute for cited excerpts. '
            'Respect the requested as-of date and jurisdiction; disclose unknown applicability, conflicts and missing '
            'verification. Do not describe the collection as current law or treat no search hit as no amendment. '
            'If a missing check prevents the conclusion, qualify or abstain. Include a concise collection limitation.\n'
            + json.dumps(review, ensure_ascii=False) + '\n\n')
