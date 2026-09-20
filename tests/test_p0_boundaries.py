from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import ingest_service
from app import app
from auth import CurrentUser, get_current_user
from chunking import chunk_document, page_units


@pytest.mark.parametrize('text', [
    '1. Permit fee - 100 rupees',
    'Rule 12 - Effective from 1 January 2024',
    'Section 3 - Except bamboo, approval is required',
])
@pytest.mark.parametrize('multiline', [False, True])
def test_heading_normalization_preserves_source_facts(text, multiline):
    content = text + ('\nApplications require proof of ownership.' if multiline else '')
    units, _heading = page_units(content, 1)
    assert text in [unit['text'] for unit in units]


def test_faq_heading_content_is_not_discarded():
    doc = {'source': 'faq.txt', 'kind': 'txt', 'title': 'FAQ', 'pages': [
        {'page': 1, 'text': 'Section 3 - Except bamboo, approval is required\nWhat is required?\nAnswer: A permit.'},
    ]}
    chunks = chunk_document(doc, profile='faq')
    assert 'Section 3 - Except bamboo, approval is required' in '\n'.join(c['content'] for c in chunks)


def test_invalid_overlap_returns_422_without_echoing_input():
    app.dependency_overrides[get_current_user] = lambda: CurrentUser('admin', 'admin@example.test', 'admin')
    try:
        response = TestClient(app).post('/admin/rag-lab/experiments', json={
            'name': 'private experiment', 'config': {'chunking': {'max_tokens': 100, 'overlap_tokens': 100}},
        })
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 422
    error = response.json()['error']
    assert error['code'] == 'validation_error'
    assert error['details']['errors'][0]['type'] == 'value_error'
    assert 'private experiment' not in response.text
    assert 'input' not in error['details']['errors'][0]
    assert 'ctx' not in error['details']['errors'][0]


def test_validation_error_does_not_echo_password():
    response = TestClient(app).post('/auth/login', json={'email': 'bad', 'password': {'secret': 'do-not-echo'}})
    assert response.status_code == 422
    assert 'do-not-echo' not in response.text


@pytest.mark.parametrize('failure', ['embedding', 'insert', 'publish', 'empty'])
def test_failed_build_never_mutates_active_chunks(monkeypatch, failure):
    active = ['old indexed evidence']
    events = []
    def insert(rows):
        assert active == ['old indexed evidence']
        if failure == 'insert':
            raise RuntimeError('insert failed')
        events.append('staged')
        return len(rows)
    def publish(*args, **kwargs):
        assert failure == 'publish'
        raise RuntimeError('publish failed')
    def embed(*args):
        if failure == 'embedding':
            raise RuntimeError('embedding failed')
        return {'content': 'replacement'}
    repo = SimpleNamespace(
        index_lease=lambda: nullcontext(SimpleNamespace(token="token", check=lambda: None)),
        indexed_sources=lambda: set(),
        begin_revision=lambda doc: {'id': 'revision', 'document_id': 'document'},
        insert_revision_chunks=insert,
        publish_revision=publish,
        fail_revision=lambda *args, **kwargs: events.append('failed'),
    )
    monkeypatch.setattr(ingest_service, 'iter_documents', lambda **kw: iter([{'source': 'rules.txt'}]))
    monkeypatch.setattr(ingest_service, 'iter_document_chunks', lambda doc: iter([] if failure == 'empty' else [{}]))
    monkeypatch.setattr(ingest_service, 'chunk_row', embed)
    with pytest.raises(Exception, match='failed|no searchable chunks'):
        ingest_service.build_index(repo)
    assert active == ['old indexed evidence']
    assert events[-1] == 'failed'
