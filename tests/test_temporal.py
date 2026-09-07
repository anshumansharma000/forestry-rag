from datetime import date

import prompts
import retrieval
from documents import extract_document_metadata
from temporal import applicable_date, extract_temporal_metadata, historical_question


def candidate(identifier, issued=None, effective=None, score=0.5):
    return {
        'id': identifier, 'source': f'{identifier}.pdf', 'text': 'Rule 12 permit fee is 100.',
        'base_score': score, 'hybrid_rank': None, 'section_heading': 'Rule 12', 'chunk_type': 'section',
        'metadata': {'issued_date': issued, 'effective_date': effective, 'temporal_metadata_version': 1},
    }


def test_extracts_explicit_dates_without_treating_referenced_year_as_date():
    metadata = extract_document_metadata('rules.pdf', 'Rules', [{'text':
        'Dated: 12 June 2023\nEffective from 1 July 2023\nAmends Rule 12 of the 2019 Rules. Review in 2030.'}])
    assert metadata['issued_date'] == '2023-06-12'
    assert metadata['effective_date'] == '2023-07-01'
    assert metadata['amendment_references'] == ['Amends Rule 12 of the 2019 Rules. Review in 2030.']


def test_ambiguous_and_referenced_dates_stay_unknown():
    assert 'issued_date' not in extract_temporal_metadata('Dated 1 June 2019\nDated 1 June 2023')
    assert 'issued_date' not in extract_temporal_metadata('Amends notification dated 1 June 2019')
    assert 'issued_date' not in extract_temporal_metadata('Rules 2019, reviewed in 2023')
    assert 'issued_date' not in extract_temporal_metadata('Dated 31 February 2023')


def test_newer_equally_relevant_evidence_ranks_first():
    ranked = retrieval.rerank_candidates('Rule 12 permit fee', [candidate('old', '2019-01-01'), candidate('new', '2023-01-01')])
    assert ranked[0]['id'] == 'new'
    assert ranked[0]['metadata']['retrieval']['recency_boost'] > ranked[1]['metadata']['retrieval']['recency_boost']


def test_recency_does_not_override_stronger_relevance_or_historical_queries():
    ranked = retrieval.rerank_candidates('Rule 12 permit fee', [candidate('old', '2019-01-01', score=0.8), candidate('new', '2023-01-01')])
    assert ranked[0]['id'] == 'old'
    ranked = retrieval.rerank_candidates('Rule 12 fee as of 2019', [candidate('old', '2019-01-01'), candidate('new', '2023-01-01')])
    assert all(c['metadata']['retrieval']['recency_boost'] == 0 for c in ranked)


def test_future_effective_provisions_get_no_current_recency_boost():
    assert applicable_date({'issued_date': '2023-01-01', 'effective_date': '2099-01-01'}, date(2026, 1, 1)) is None
    ranked = retrieval.rerank_candidates('Rule 12 permit fee', [candidate('future', '2023-01-01', '2099-01-01')])
    assert ranked[0]['metadata']['retrieval']['recency_boost'] == 0


def test_named_rule_year_does_not_disable_current_updates():
    assert not historical_question('Current permit fee under Forest Rules, 2019')
    assert historical_question('Permit fee under Forest Rules, 2019 as of 2020')


def test_existing_chunk_uses_explicit_text_dates_only():
    chunk = candidate('legacy')
    chunk['metadata'] = {'years': ['2019', '2099'], 'updated_at': '2099-01-01'}
    chunk['text'] = 'Dated: 1 June 2023\nRule 12 permit fee.'
    assert retrieval.rerank_candidates('permit fee', [chunk])[0]['metadata']['issued_date'] == '2023-06-01'


def test_final_prompt_contains_dates_and_precedence_rules(monkeypatch):
    captured = []
    monkeypatch.setattr(prompts, 'retrieval_is_confident', lambda _: True)
    monkeypatch.setattr(prompts, 'generate_with_gemini', lambda prompt: captured.append(prompt) or 'Fee: 100 [1]')
    assert prompts.answer_with_gemini('What is the fee?', [candidate('new', '2023-01-01')]) == 'Fee: 100 [1]'
    assert 'Issue date: 2023-01-01' in captured[0]
    assert 'retain unchanged older provisions' in captured[0]
    assert 'future-effective' in captured[0]
    assert 'historical or as-of questions' in captured[0]


def test_update_search_merges_amendment_before_context_selection(monkeypatch):
    queries = []
    def row(identifier, issued):
        return {**candidate(identifier, issued), 'document_id': identifier,
                'chunk_index': 0, 'page_start': 1, 'page_end': 1,
                'content': 'Rule 12 permit fee is 100.', 'similarity': 0.5}
    class Repository:
        def match_chunks(self, embedding, query, count):
            queries.append(query)
            return [row('new', '2023-01-01')] if 'supersession' in query else [row('old', '2019-01-01')]
    monkeypatch.setattr(retrieval, 'embed_query', lambda _: [1.0])
    contexts = retrieval.retrieve('Rule 12 permit fee', top_k=1, repository=Repository(), options={'expand_neighbors': False})
    assert len(queries) == 2
    assert contexts[0]['id'] == 'new'
