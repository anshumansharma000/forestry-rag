from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import routers.documents as routes
import services.document_storage as storage_module
from app import app
from auth import CurrentUser, get_authenticated_user
from consistency import OperationBusy
from errors import AppError, ErrorCode
from services.document_storage import LocalDocumentStorage, R2DocumentStorage

DOCUMENT_ID = str(uuid4())
client = TestClient(app)


@pytest.fixture
def deletion(monkeypatch):
    events = []
    state = SimpleNamespace(busy=False, missing=False, storage_error=False, database_error=False)

    class Repository:
        @contextmanager
        def index_lease(self):
            events.append('lock')
            try:
                yield SimpleNamespace(token='lease-token', check=lambda: events.append('check'))
            finally:
                events.append('unlock')

        def prepare_delete(self, document_id, token):
            events.append('prepare')
            assert document_id == DOCUMENT_ID and token == 'lease-token'
            if state.busy:
                raise OperationBusy()
            if state.missing:
                raise AppError('Document not found.', code=ErrorCode.NOT_FOUND, status_code=404)
            return {'source': 'rules.pdf'}

        def finish_delete(self, document_id, token, audit):
            events.append('finish')
            assert document_id == DOCUMENT_ID and token == 'lease-token'
            assert audit['actor_role'] == 'admin'
            if state.database_error:
                raise RuntimeError('database unavailable')

    class Storage:
        def delete(self, source):
            events.append('storage')
            assert source == 'rules.pdf'
            if state.storage_error:
                raise AppError('Storage unavailable.', code=ErrorCode.STORAGE_ERROR, status_code=503)

    monkeypatch.setattr(routes, 'DocumentRepository', Repository)
    monkeypatch.setattr(routes, 'document_storage', Storage)
    app.dependency_overrides[get_authenticated_user] = lambda: CurrentUser(id=str(uuid4()), email='admin@example.com', role='admin')
    yield state, events
    app.dependency_overrides.clear()


def test_admin_deletion_orders_storage_before_atomic_database_cleanup(deletion):
    _, events = deletion
    response = client.delete(f'/documents/{DOCUMENT_ID}', headers={'Authorization': 'Bearer token'})
    assert response.status_code == 200
    assert response.json() == {'status': 'deleted', 'document_id': DOCUMENT_ID}
    assert events == ['lock', 'prepare', 'check', 'storage', 'finish', 'unlock']


@pytest.mark.parametrize('role', ['viewer', 'officer', 'knowledge_manager'])
def test_non_admin_cannot_delete(deletion, role):
    _, events = deletion
    app.dependency_overrides[get_authenticated_user] = lambda: CurrentUser(id='user', email='user@example.com', role=role)
    assert client.delete(f'/documents/{DOCUMENT_ID}', headers={'Authorization': 'Bearer token'}).status_code == 403
    assert events == []


def test_bearer_is_required_even_with_auth_disabled(monkeypatch):
    monkeypatch.setenv('AUTH_DISABLED', 'true')
    assert client.delete(f'/documents/{DOCUMENT_ID}').status_code == 401


@pytest.mark.parametrize(('flag', 'status'), [('busy', 409), ('missing', 404)])
def test_preflight_errors_leave_storage_untouched(deletion, flag, status):
    state, events = deletion
    setattr(state, flag, True)
    assert client.delete(f'/documents/{DOCUMENT_ID}', headers={'Authorization': 'Bearer token'}).status_code == status
    assert 'storage' not in events and 'finish' not in events


def test_storage_failure_keeps_database_and_allows_retry(deletion):
    state, events = deletion
    state.storage_error = True
    assert client.delete(f'/documents/{DOCUMENT_ID}', headers={'Authorization': 'Bearer token'}).status_code == 503
    assert 'finish' not in events
    state.storage_error = False
    assert client.delete(f'/documents/{DOCUMENT_ID}', headers={'Authorization': 'Bearer token'}).status_code == 200


def test_database_failure_explains_partial_cleanup_and_allows_retry(deletion):
    state, _ = deletion
    state.database_error = True
    response = client.delete(f'/documents/{DOCUMENT_ID}', headers={'Authorization': 'Bearer token'})
    assert response.status_code == 503
    assert 'Retry Delete' in response.json()['error']['message']
    state.database_error = False
    assert client.delete(f'/documents/{DOCUMENT_ID}', headers={'Authorization': 'Bearer token'}).status_code == 200


def test_local_delete_is_idempotent_and_confines_paths(monkeypatch, tmp_path):
    root = tmp_path / 'docs'
    root.mkdir()
    monkeypatch.setattr(storage_module, 'DOCS_DIR', root)
    outside = tmp_path / 'outside.pdf'
    outside.write_bytes(b'keep')
    (root / 'rules.pdf').write_bytes(b'delete')
    storage = LocalDocumentStorage()
    storage.delete('rules.pdf')
    storage.delete('rules.pdf')
    (root / 'link.pdf').symlink_to(outside)
    for name in ['../outside.pdf', str(outside), '.', '..', '', 'link.pdf']:
        with pytest.raises(AppError):
            storage.delete(name)
    assert outside.read_bytes() == b'keep'
    assert not (root / 'rules.pdf').exists()


def test_r2_delete_uses_configured_prefix_and_wraps_storage_errors():
    calls = []
    storage = object.__new__(R2DocumentStorage)
    storage.bucket, storage.prefix = 'bucket', 'docs/'
    storage.client = SimpleNamespace(delete_object=lambda **kwargs: calls.append(kwargs))
    storage.delete('rules.pdf')
    storage.delete('rules.pdf')
    assert calls == [{'Bucket': 'bucket', 'Key': 'docs/rules.pdf'}] * 2
    for name in ['../outside.pdf', '/outside.pdf', '.', '..', '']:
        with pytest.raises(AppError):
            storage.delete(name)
    def fail(**kwargs):
        raise RuntimeError('R2 unavailable')
    storage.client.delete_object = fail
    with pytest.raises(AppError) as error:
        storage.delete('rules.pdf')
    assert error.value.status_code == 503


def test_missing_deletion_migration_is_actionable_and_never_touches_storage(monkeypatch):
    from postgrest.exceptions import APIError

    from app import create_app
    from repositories import DocumentRepository

    class Client:
        def rpc(self, function, params):
            assert function == 'prepare_document_delete'
            return self

        def execute(self):
            raise APIError({'code': 'PGRST202', 'message': 'Function missing', 'details': None, 'hint': None})

    class Repository(DocumentRepository):
        def __init__(self):
            super().__init__(Client())

        @contextmanager
        def index_lease(self):
            yield SimpleNamespace(token='lease', check=lambda: None)

    monkeypatch.setattr(routes, 'DocumentRepository', Repository)
    monkeypatch.setattr(routes, 'document_storage', lambda: pytest.fail('Must not access storage before preflight succeeds'))
    monkeypatch.setenv('CORS_ALLOWED_ORIGINS', 'http://localhost:3000')
    local_app = create_app()
    local_app.dependency_overrides[get_authenticated_user] = lambda: CurrentUser(
        id=str(uuid4()), email='admin@example.com', role='admin')
    response = TestClient(local_app).delete(f'/documents/{DOCUMENT_ID}', headers={
        'Authorization': 'Bearer token', 'Origin': 'http://localhost:3000'})
    assert response.status_code == 503
    assert response.headers['access-control-allow-origin'] == 'http://localhost:3000'
    assert response.json()['error']['code'] == 'config_error'
    assert 'migration 017' in response.json()['error']['message']
    assert response.json()['error']['details']['migration'] == '017_admin_document_deletion.sql'
