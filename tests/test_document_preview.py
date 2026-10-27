from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import routers.documents as routes
from app import create_app
from auth import CurrentUser, get_authenticated_user
from repositories import DocumentRepository

DOCUMENT_ID = str(uuid4())


@pytest.fixture
def preview(monkeypatch):
    state = SimpleNamespace(document={'id': DOCUMENT_ID, 'source': 'rules.docx'}, rows=[], calls=[], audits=[])

    class Repository:
        def get_document(self, document_id):
            assert document_id == DOCUMENT_ID
            return state.document

        def preview_chunks(self, document_id, offset, limit):
            state.calls.append((document_id, offset, limit))
            return state.rows

    monkeypatch.setattr(routes, 'DocumentRepository', Repository)
    monkeypatch.setattr(routes, 'audit_event', lambda *args: state.audits.append(args))
    app = create_app()
    app.dependency_overrides[get_authenticated_user] = lambda: CurrentUser(str(uuid4()), 'admin@example.com', 'admin')
    state.app, state.client = app, TestClient(app)
    return state


def test_admin_views_bounded_indexed_text_and_can_page_forward(preview):
    preview.rows = [{'chunk_index': i, 'content': f'Paragraph {i}', 'page_start': i + 1} for i in range(3)]
    response = preview.client.get(f'/documents/{DOCUMENT_ID}/preview?offset=5&limit=2', headers={'Authorization': 'Bearer token'})
    assert response.status_code == 200
    data = response.json()
    assert data['filename'] == 'rules.docx'
    assert data['sections'] == preview.rows[:2]
    assert data['next_offset'] == 7
    assert preview.calls == [(DOCUMENT_ID, 5, 2)]
    assert preview.audits[0][2] == 'documents.preview'


def test_final_page_has_no_next_offset(preview):
    preview.rows = [{'chunk_index': 0, 'content': 'Last paragraph'}]
    response = preview.client.get(f'/documents/{DOCUMENT_ID}/preview', headers={'Authorization': 'Bearer token'})
    assert response.json()['next_offset'] is None


@pytest.mark.parametrize('role', ['viewer', 'officer', 'knowledge_manager'])
def test_preview_keeps_existing_admin_only_original_access(preview, role):
    preview.app.dependency_overrides[get_authenticated_user] = lambda: CurrentUser('user', 'user@example.com', role)
    response = preview.client.get(f'/documents/{DOCUMENT_ID}/preview', headers={'Authorization': 'Bearer token'})
    assert response.status_code == 403
    assert preview.calls == []


def test_preview_requires_bearer_even_when_auth_disabled(preview, monkeypatch):
    monkeypatch.setenv('AUTH_DISABLED', 'true')
    preview.app.dependency_overrides.clear()
    assert preview.client.get(f'/documents/{DOCUMENT_ID}/preview').status_code == 401


def test_missing_document_and_unindexed_document_have_clear_errors(preview):
    response = preview.client.get(f'/documents/{DOCUMENT_ID}/preview', headers={'Authorization': 'Bearer token'})
    assert response.status_code == 404
    assert 'Process this document first' in response.json()['error']['message']
    preview.document = None
    response = preview.client.get(f'/documents/{DOCUMENT_ID}/preview', headers={'Authorization': 'Bearer token'})
    assert response.status_code == 404
    assert preview.calls == [(DOCUMENT_ID, 0, 40)]


@pytest.mark.parametrize('query', ['offset=-1', 'limit=0', 'limit=101'])
def test_preview_rejects_unbounded_or_negative_pages(preview, query):
    assert preview.client.get(f'/documents/{DOCUMENT_ID}/preview?{query}', headers={'Authorization': 'Bearer token'}).status_code == 422
    assert preview.calls == []


def test_repository_reads_published_chunks_in_order_without_vectors():
    calls = []

    class Query:
        def select(self, fields):
            calls.append(('select', fields))
            return self

        def eq(self, field, value):
            calls.append(('eq', field, value))
            return self

        def order(self, field):
            calls.append(('order', field))
            return self

        def range(self, start, end):
            calls.append(('range', start, end))
            return self

        def execute(self):
            return SimpleNamespace(data=[{'content': 'Published content'}])

    def table(name):
        calls.append(('table', name))
        return Query()

    result = DocumentRepository(SimpleNamespace(table=table)).preview_chunks(DOCUMENT_ID, 40, 40)
    assert result == [{'content': 'Published content'}]
    assert ('table', 'document_chunks') in calls
    assert ('eq', 'document_id', DOCUMENT_ID) in calls
    assert ('range', 40, 80) in calls
    assert ('order', 'chunk_index') in calls
    assert 'embedding' not in calls[1][1]
