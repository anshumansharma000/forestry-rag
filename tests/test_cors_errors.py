import pytest
from fastapi.testclient import TestClient

from app import create_app
from errors import AppError, ErrorCode


@pytest.fixture
def cors_app(monkeypatch):
    monkeypatch.setenv('CORS_ALLOWED_ORIGINS', 'http://localhost:3000,http://127.0.0.1:3000')
    monkeypatch.setenv('RATE_LIMIT_ENABLED', 'false')
    app = create_app()

    @app.delete('/test-delete-error')
    def delete_error():
        raise RuntimeError('private database diagnostics')

    @app.delete('/test-delete-handled')
    def delete_handled():
        raise AppError('Apply migration 017.', code=ErrorCode.CONFIG_ERROR, status_code=503)

    return app


@pytest.mark.parametrize('origin', ['http://localhost:3000', 'http://127.0.0.1:3000'])
def test_unhandled_500_remains_readable_by_allowed_frontend(cors_app, origin):
    response = TestClient(cors_app, raise_server_exceptions=False).delete('/test-delete-error', headers={'Origin': origin})
    assert response.status_code == 500
    assert response.headers['access-control-allow-origin'] == origin
    assert response.headers['access-control-allow-credentials'] == 'true'
    assert 'Origin' in response.headers['vary']
    assert response.json()['error']['code'] == 'internal_error'
    assert 'private database diagnostics' not in response.text


def test_untrusted_origin_does_not_gain_access_to_errors(cors_app):
    response = TestClient(cors_app, raise_server_exceptions=False).delete(
        '/test-delete-error', headers={'Origin': 'https://untrusted.example'})
    assert response.status_code == 500
    assert 'access-control-allow-origin' not in response.headers


def test_delete_preflight_and_actionable_errors_share_cors_policy(cors_app):
    client = TestClient(cors_app)
    origin = 'http://localhost:3000'
    preflight = client.options('/test-delete-handled', headers={
        'Origin': origin, 'Access-Control-Request-Method': 'DELETE',
        'Access-Control-Request-Headers': 'authorization,content-type',
    })
    assert preflight.status_code == 200
    assert preflight.headers['access-control-allow-origin'] == origin
    assert 'DELETE' in preflight.headers['access-control-allow-methods']
    response = client.delete('/test-delete-handled', headers={'Origin': origin})
    assert response.status_code == 503
    assert response.headers['access-control-allow-origin'] == origin
    assert response.json()['error']['message'] == 'Apply migration 017.'


def test_security_middleware_errors_also_have_cors(cors_app, monkeypatch):
    monkeypatch.setenv('REQUEST_TARGET_MAX_BYTES', '10')
    response = TestClient(cors_app).delete('/test-delete-error', headers={'Origin': 'http://localhost:3000'})
    assert response.status_code == 414
    assert response.headers['access-control-allow-origin'] == 'http://localhost:3000'
