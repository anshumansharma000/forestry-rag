import logging

import pytest

import legal_retrieval
import prompts
import token_usage
from model_routing import extraction_eligible
from tests.test_legal_hierarchy import Corpus, Registry, row


def source(text='Submit Form A and proof of ownership to the officer. Approval is required before transport.'):
    return {'id': 'a', 'document_id': 'rules', 'source': 'rules.pdf', 'section_heading': 'Procedure',
            'text': text, 'metadata': {}, 'score': .9, 'base_score': .9}


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.setenv('RAG_LITE_EXTRACTION', 'true')
    monkeypatch.setenv('RAG_RISK_BASED_VERIFICATION', 'true')
    monkeypatch.setenv('RAG_COST_OPTIMIZATIONS', 'true')


def test_routine_procedure_uses_lite_without_plan(policy):
    question = 'List the application steps.'
    assert prompts.answer_generation_operation(question, [source()]) == 'answer_direct'
    assert not prompts.needs_evidence_plan(question, [source()])


@pytest.mark.parametrize('question', [
    'Which rule is in force?', 'List the steps applicable to my case.', 'Summarize the court direction.',
    'List the steps as of 2020.', 'Which rule applies after the amendment?', 'What documents are currently required?',
])
def test_legal_reconciliation_not_extraction(policy, question):
    assert not extraction_eligible(question, [source()])
    assert prompts.answer_generation_operation(question, [source()]) == 'answer_complex_high'


@pytest.mark.parametrize('text', [
    'Subject to Rule 16, submit Form A.', 'Amends Rule 5.', 'Read with section 2.',
    'Bamboo is fee-exempt but needs approval.', '',
])
def test_cross_instrument_conditions_retain_flash(policy, text):
    assert not extraction_eligible('List the application steps.', [source(text)])


def test_multiple_sources_and_review_gaps_not_eligible(policy):
    ctx = source()
    assert not extraction_eligible('List the steps.', [ctx, {**ctx, 'document_id': 'other'}])
    ctx['metadata'] = {'legal_review': {'missing_reviewed_evidence': ['judicial']}}
    assert not extraction_eligible('List the steps.', [ctx])


def test_lite_extraction_requires_audit_even_with_general_audit_disabled(policy, monkeypatch):
    calls = []
    monkeypatch.setenv('RAG_ANSWER_VERIFICATION', 'false')
    monkeypatch.setattr(prompts, 'generate_with_gemini', lambda *a, **kw: 'Submit Form A [1].')
    def audit(*args, **kwargs):
        calls.append(kwargs)
        return {'supported': True, 'unchanged': True, 'answer': '', 'issues': []}
    monkeypatch.setattr(prompts, 'generate_structured_with_gemini', audit)
    assert 'Form A' in prompts.answer_with_gemini('List the application steps.', [source()])
    assert [call['operation'] for call in calls] == ['verify']


def test_high_risk_direct_question_goes_straight_to_flash_audit(policy, monkeypatch):
    operations = []
    monkeypatch.setattr(prompts, 'generate_with_gemini', lambda *a, **kw: 'Approval is required [1].')
    def audit(*args, **kw):
        operations.append(kw['operation'])
        return {'supported': True, 'unchanged': True, 'answer': '', 'issues': []}
    monkeypatch.setattr(prompts, 'generate_structured_with_gemini', audit)
    prompts.answer_with_gemini('Which rule is in force?', [source()])
    assert operations == ['verify_complex']


def test_lite_rejection_escalates_once(policy, monkeypatch):
    operations = []
    def audit(*a, **kw):
        operations.append(kw['operation'])
        return {'supported': False, 'unchanged': False, 'answer': '', 'issues': ['unsupported']}
    monkeypatch.setattr(prompts, 'generate_structured_with_gemini', audit)
    assert prompts.verify_answer_with_gemini('List steps', 'Unsupported [1]', 'evidence') == prompts.UNSUPPORTED_ANSWER
    assert operations == ['verify', 'verify_complex']


def test_rollback_restores_procedure_route(policy, monkeypatch):
    monkeypatch.setenv('RAG_LITE_EXTRACTION', 'false')
    assert prompts.answer_generation_operation('List the application steps.', [source()]) == 'answer_complex'


def test_legal_batch_preserves_six_search_stages(monkeypatch):
    calls = []
    monkeypatch.setenv('RAG_LEGAL_BATCH_EMBEDDINGS', 'true')
    monkeypatch.setattr(legal_retrieval, 'embed_query', lambda text: calls.append([text]) or [1.])
    monkeypatch.setattr(legal_retrieval, 'embed_queries', lambda texts: calls.append(texts) or [[1.] for _ in texts])
    repo, registry = Corpus([row('a')]), Registry()
    legal_retrieval.retrieve_legal('Forest diversion', repository=repo, registry=registry)
    assert [len(c) for c in calls] == [1, 5]
    assert len(repo.calls) == len(registry.calls) == 6


def test_embedding_missing_usage_is_unknown_and_preserves_generation_fields(caplog):
    @token_usage.track_query_usage
    def query():
        token_usage.record_embedding('embedding-model', {})
    with caplog.at_level(logging.INFO, logger='token_usage'):
        query()
    record = next(r for r in caplog.records if r.message == 'query_generation_usage')
    assert record.embedding_calls == 1
    assert record.total_api_cost_usd is None
    assert not record.total_api_cost_complete
    assert record.estimated_cost_usd == 0  # Existing generation-only field retained.


def test_embedding_pricing_requires_matching_model(monkeypatch, caplog):
    monkeypatch.setenv('COST_EMBEDDING_RATE_MODEL', 'embedding-model')
    monkeypatch.setenv('COST_EMBEDDING_USD_PER_MILLION_TOKENS', '0.5')
    @token_usage.track_query_usage
    def query():
        token_usage.record_embedding('embedding-model', {'promptTokenCount': 1000}, items=5)
    with caplog.at_level(logging.INFO, logger='token_usage'):
        query()
    record = next(r for r in caplog.records if r.message == 'query_generation_usage')
    assert record.total_api_cost_usd == .0005
    assert record.embedding_items == 5


def test_history_cannot_silently_downgrade_a_followup(policy):
    assert prompts.answer_generation_operation('List the application steps.', [source()], allow_extraction=False) == 'answer_complex'
