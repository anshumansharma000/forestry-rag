from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import legal_retrieval
import retrieval
import routers.legal as legal_router
from app import app
from auth import CurrentUser, get_authenticated_user
from legal_registry import STAGES, LegalProfile, LegalRegistryRepository, legal_namespace
from legal_retrieval import related_documents, retrieve_legal
from prompts import format_contexts
from rag_errors import RagError
from schemas import AskRequest, AskResponse, ChatAskRequest, SourceResponse


def row(name, text='Forest diversion requires prior approval under section 2.', score=0.7):
    return {'id': name, 'document_id': name, 'source': f'{name}.pdf', 'chunk_index': 0,
            'chunk_type': 'section', 'section_heading': 'Section 2', 'page_start': 1,
            'page_end': 1, 'content': text, 'similarity': score, 'metadata': {}}


class Corpus:
    def __init__(self, rows=()):
        self.rows = rows
        self.calls = []

    def match_chunks(self, embedding, query, count):
        self.calls.append(query)
        return deepcopy(self.rows)


class Registry:
    def __init__(self, profiles=None, rows=None):
        self.catalog = profiles or {}
        self.rows = rows or {}
        self.calls = []

    def profiles(self):
        return deepcopy(self.catalog)

    def match(self, embedding, query, kinds, count, document_ids=None):
        self.calls.append((query, kinds, document_ids))
        return deepcopy([r for kind in document_ids or kinds for r in self.rows.get(kind, [])])


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv('RAG_LEGAL_HIERARCHY', raising=False)
    monkeypatch.delenv('LEGAL_REGISTRY_NAMESPACE', raising=False)
    monkeypatch.delenv('LEGAL_REGISTRY_WRITES_ENABLED', raising=False)
    monkeypatch.setattr(legal_retrieval, 'embed_query', lambda _: [1.0])
    # These tests must never obtain the configured Supabase client.
    monkeypatch.setattr('legal_registry.supabase_client', lambda: pytest.fail('Unexpected database access'))
    yield
    app.dependency_overrides.clear()


def test_legacy_requests_and_sources_keep_existing_shape():
    assert AskRequest(question='Forest?').model_dump() == {'question': 'Forest?', 'top_k': None}
    assert ChatAskRequest(message='Forest?').model_dump() == {'message': 'Forest?', 'top_k': None, 'request_id': None}
    assert set(AskResponse.model_fields) == {'answer', 'sources', 'cited_sources', 'confidence', 'abstained', 'outcome'}
    assert 'legal_profile' not in SourceResponse.model_fields


def test_default_path_never_opens_registry(monkeypatch):
    monkeypatch.setattr(retrieval, 'embed_query', lambda _: [1.0])
    monkeypatch.setattr(legal_retrieval, 'retrieve_legal', lambda *a: pytest.fail('Opt-in path invoked'))
    assert retrieval.retrieve('Forest diversion', repository=Corpus(), options={'expand_neighbors': False}) == []


def test_enabled_path_routes_without_changing_signature(monkeypatch):
    monkeypatch.setenv('RAG_LEGAL_HIERARCHY', 'true')
    expected = [row('a')]
    monkeypatch.setattr(legal_retrieval, 'retrieve_legal', lambda *args: expected)
    assert retrieval.retrieve('Forest diversion', 3, Corpus()) is expected


@pytest.mark.parametrize('value', ['', ' ', '../prod', 'a' * 65])
def test_explicit_namespace_required(monkeypatch, value):
    monkeypatch.setenv('LEGAL_REGISTRY_NAMESPACE', value)
    with pytest.raises(RagError):
        legal_namespace()


def test_every_stage_searched_without_handbook_or_catalog():
    registry, corpus = Registry(), Corpus([row('legacy')])
    contexts = retrieve_legal('Forest diversion', repository=corpus, registry=registry)
    assert len(registry.calls) == len(STAGES)
    assert len(corpus.calls) == len(STAGES)
    assert registry.calls[-1][1] == ['judicial']
    assert contexts[0]['document_id'] == 'legacy'
    review = contexts[0]['metadata']['legal_review']
    assert set(review['missing_reviewed_evidence']) == set(STAGES)
    assert review['collection_currentness'] == 'unverified'
    assert 'no search hit as no amendment' in format_contexts(contexts)


def test_judicial_evidence_survives_high_scoring_handbook_and_small_budget():
    registry = Registry(
        {'h': {'instrument_type': 'handbook', 'reviewed': True},
         'j': {'instrument_type': 'judicial', 'reviewed': True}},
        {'handbook': [row('h', score=.99)], 'judicial': [row('j', score=.4)]},
    )
    contexts = retrieve_legal('Forest diversion', top_k=1, repository=Corpus(), registry=registry)
    assert len(contexts) == 1
    assert contexts[0]['document_id'] == 'j'
    assert 'handbook' in contexts[0]['metadata']['legal_review']['missing_reviewed_evidence']


def test_references_in_handbook_guide_later_searches():
    registry = Registry(rows={'handbook': [row('h', 'Forest diversion procedure Rule 16.')]})
    retrieve_legal('Forest diversion', repository=Corpus(), registry=registry)
    assert 'Rule 16' in registry.calls[1][0]


def test_draft_dates_do_not_override_legacy_metadata():
    registry = Registry({'a': {'effective_date': '2099-01-01', 'reviewed': False}})
    contexts = retrieve_legal('Forest diversion', repository=Corpus([row('a')]), registry=registry)
    assert contexts[0]['metadata'].get('effective_date') is None


def test_historical_scope_is_not_replaced_by_today():
    contexts = retrieve_legal('Forest diversion as of 2005', repository=Corpus([row('a')]), registry=Registry())
    assert 'historical' in contexts[0]['metadata']['legal_review']['as_of']
    contexts = retrieve_legal('Forest diversion', repository=Corpus([row('a')]), registry=Registry(),
                              options={'as_of': '2005-01-01', 'jurisdiction': 'Goa'})
    assert contexts[0]['metadata']['legal_review']['as_of'] == '2005-01-01'
    assert contexts[0]['metadata']['legal_review']['jurisdiction'] == 'Goa'
    assert contexts[0]['metadata']['retrieval']['recency_boost'] == 0


def test_relationships_follow_incoming_amendments_and_cycles():
    profiles = {'amendment': {'reviewed': True, 'relationships': [{'target_document_id': 'act'}]},
                'act': {'reviewed': True, 'relationships': [{'target_document_id': 'amendment'}]},
                'draft': {'reviewed': False, 'relationships': [{'target_document_id': 'act'}]}}
    assert related_documents({'act'}, profiles) == (['amendment'], False)
    assert related_documents({'act'}, profiles, limit=0) == ([], True)


def test_relationship_retrieval_fetches_incoming_update():
    registry = Registry({'a': {'reviewed': True, 'relationships': [{'target_document_id': 'base'}]}},
                        {'a': [row('a')]})
    contexts = retrieve_legal('Forest diversion', repository=Corpus([row('base')]), registry=registry)
    assert any(call[2] == ['a'] for call in registry.calls)
    assert any(ctx['document_id'] == 'a' for ctx in contexts)


def test_low_score_and_irrelevant_hits_not_claimed_as_coverage():
    registry = Registry({'j': {'instrument_type': 'judicial', 'reviewed': True}},
                        {'judicial': [row('j', 'Shipping tariffs and ocean trade.')]})
    assert retrieve_legal('Forest diversion', repository=Corpus(), registry=registry) == []
    assert retrieve_legal('Forest diversion', repository=Corpus([row('a', score=.01)]), registry=Registry(),
                          options={'min_context_score': .9}) == []


def authenticate(role='admin'):
    app.dependency_overrides[get_authenticated_user] = lambda: CurrentUser(
        id='test-user', email='test@example.com', role=role)


@pytest.mark.parametrize('role', ['viewer', 'officer', 'knowledge_manager'])
def test_new_endpoints_require_admin(role):
    authenticate(role)
    response = TestClient(app).post('/admin/legal/preview', json={'question': 'Forest diversion'})
    assert response.status_code == 403


def test_writes_default_disabled_even_for_admin():
    authenticate()
    response = TestClient(app).put(f'/admin/legal/profiles/{uuid4()}', json={})
    assert response.status_code == 403


def test_preview_does_not_write_chat_or_annotations(monkeypatch):
    authenticate()
    contexts = retrieve_legal('Forest diversion', repository=Corpus([row('a')]), registry=Registry())
    monkeypatch.setattr(legal_router, 'retrieve_legal', lambda *a, **kw: contexts)
    monkeypatch.setattr(legal_router, 'answer_with_gemini', lambda *a: pytest.fail('Generation not requested'))
    response = TestClient(app).post('/admin/legal/preview', json={'question': 'Forest diversion'})
    assert response.status_code == 200
    assert response.json()['review']['collection_currentness'] == 'unverified'
    assert response.json()['sources'][0]['document_id'] == 'a'


def test_old_schema_compatibility_migration_has_no_legacy_mutations():
    sql = Path('migrations/010_legal_registry.sql').read_text().lower()
    assert 'alter table documents' not in sql
    assert 'alter table document_chunks' not in sql
    assert 'create or replace function match_document_chunks' not in sql
    assert 'update documents' not in sql
    assert 'enable row level security' in sql


def test_profile_defaults_accept_missing_optional_fields():
    assert LegalProfile().model_dump()['reviewed'] is False
    assert LegalProfile(instrument_type='act').effective_date is None


def test_repository_scopes_reads_and_writes_to_server_namespace(monkeypatch):
    calls = []

    class Client:
        data = []

        def table(self, name):
            calls.append(('table', name))
            return self

        def __getattr__(self, method):
            def query(*args, **kwargs):
                calls.append((method, args, kwargs))
                if method == 'upsert':
                    self.data = [args[0]]
                return self
            return query

    monkeypatch.setenv('LEGAL_REGISTRY_NAMESPACE', 'dev')
    repository = LegalRegistryRepository(client=Client())
    assert repository.profiles() == {}
    assert ('eq', ('namespace', 'dev'), {}) in calls
    saved = repository.save(str(uuid4()), LegalProfile(), 'reviewer')
    assert saved['namespace'] == 'dev'
    assert all(call != ('table', 'documents') for call in calls)


def test_suggestions_never_assert_reviewed_relationships_or_dates_from_years():
    from legal_registry import suggest_profile

    suggested = suggest_profile({'source': 'Van Rules Amendment 2025.pdf', 'metadata': {'years': ['2025']}})
    assert suggested.instrument_type == 'amendment'
    assert suggested.reviewed is False
    assert suggested.relationships == []
    assert suggested.issued_date is None
    assert suggest_profile({'title': 'Consolidated Guidelines and Clarifications'}).instrument_type == 'handbook'


def test_save_checks_all_relationship_targets_without_touching_corpus(monkeypatch):
    authenticate()
    monkeypatch.setenv('LEGAL_REGISTRY_WRITES_ENABLED', 'true')
    saved = []
    doc_id, target_id = str(uuid4()), str(uuid4())

    class Documents:
        def get_document(self, identifier):
            return {'id': identifier} if identifier == doc_id else None

    class Annotations:
        def save(self, *args):
            saved.append(args)

    monkeypatch.setattr(legal_router, 'DocumentRepository', Documents)
    monkeypatch.setattr(legal_router, 'LegalRegistryRepository', Annotations)
    body = {'instrument_type': 'amendment', 'reviewed': True, 'relationships': [{
        'target_document_id': target_id, 'relation': 'amends', 'source_provision': 'paragraph 2',
        'target_provision': 'Rule 16', 'evidence': 'Rule 16 is amended as follows.'}]}
    client = TestClient(app)
    assert client.put(f'/admin/legal/profiles/{doc_id}', json=body).status_code == 404
    assert saved == []
    body['relationships'] = []
    assert client.put(f'/admin/legal/profiles/{doc_id}', json=body).status_code == 200
    assert saved[0][0] == doc_id
    assert saved[0][1].reviewed


def test_missing_related_excerpt_is_reported():
    profiles = {'amendment': {'reviewed': True, 'relationships': [{'target_document_id': 'base'}]}}
    contexts = retrieve_legal('Forest diversion', repository=Corpus([row('base')]), registry=Registry(profiles))
    review = contexts[0]['metadata']['legal_review']
    assert review['missing_relationship_count'] == 1
    assert review['relationships_missing_selected_evidence'][0]['source_document_id'] == 'amendment'


def test_disabled_mode_formats_original_prompt_identically():
    context = retrieval.context_from_row(row('old'))
    assert format_contexts([context]) == (
        '[1] Source: old.pdf, page 1\nSection: Section 2; Evidence role: matched\n'
        'Issue date: Unknown; Effective date: Unknown\n'
        'Forest diversion requires prior approval under section 2.'
    )
