import asyncio
import logging
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient

import auth
import request_limits
import security_middleware
from app import create_app
from errors import AppError, error_response
from redaction import safe_failure
from security_settings import validate_security_settings
from structured_logging import JsonLogFormatter


@pytest.fixture(autouse=True)
def development(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.delenv('RENDER', raising=False)
    monkeypatch.setenv('RATE_LIMIT_ENABLED', 'false')
    monkeypatch.setenv('AUTH_DISABLED', 'false')
    monkeypatch.setenv('JWT_SECRET_KEY', 'a-test-secret-with-at-least-32-bytes')


def test_access_tokens_and_legacy_tokens_invalidated(monkeypatch):
    row = dict(id='user-1', email='a@example.org', role='viewer', is_active=True, token_version=0)
    monkeypatch.setattr(auth, 'AuthRepository', lambda: SimpleNamespace(get_user_by_id=lambda _: row))
    token, _ = auth.create_access_token(auth._user_from_row(row))
    claims = jwt.decode(token, auth._jwt_secret(), algorithms=['HS256'])
    claims.pop('ver')
    legacy = jwt.encode(claims, auth._jwt_secret(), algorithm='HS256')
    assert auth._user_from_jwt(token).token_version == 0
    assert auth._user_from_jwt(legacy).token_version == 0
    row['token_version'] = 1
    for old in (token, legacy):
        with pytest.raises(auth.AuthError, match='revoked'):
            auth._user_from_jwt(old)
    fresh, _ = auth.create_access_token(auth._user_from_row(row))
    assert auth._user_from_jwt(fresh).token_version == 1


def test_refresh_uses_one_atomic_rpc_and_keeps_response_contract(monkeypatch):
    calls = []
    row = dict(id='user-1', email='a@example.org', role='viewer', is_active=True, token_version=3)
    def rotate(*args):
        calls.append(args)
        return {'user': row, 'expires_at': args[2]}
    monkeypatch.setattr(auth, 'AuthRepository', lambda: SimpleNamespace(rotate_refresh_token=rotate))
    result = auth.refresh_access_token('old-token')
    assert len(calls) == 1
    assert calls[0][0] == auth.hash_refresh_token('old-token')
    assert calls[0][1] == auth.hash_refresh_token(result['refresh_token'])
    assert jwt.decode(result['access_token'], auth._jwt_secret(), algorithms=['HS256'])['ver'] == 3
    assert {'access_token', 'refresh_token', 'token_type', 'user'} <= result.keys()
    assert 'token_version' not in result['user']


def test_change_password_response_uses_incremented_version(monkeypatch):
    from routers import auth_routes
    app = create_app()
    old = auth.CurrentUser('user-1', 'a@example.org', 'viewer')
    updated = auth.CurrentUser('user-1', 'a@example.org', 'viewer', token_version=1)
    app.dependency_overrides[auth.get_current_user] = lambda: old
    monkeypatch.setattr(auth_routes, 'change_password', lambda *args: {'changed': True, 'user': updated})
    monkeypatch.setattr(auth_routes, 'audit_event', lambda *args: None)
    monkeypatch.setattr(auth, 'issue_refresh_token', lambda *args: ('refresh', '2099-01-01T00:00:00Z', 'id'))
    response = TestClient(app).post('/auth/change-password', json={'current_password': 'old', 'new_password': 'newPassword123'})
    assert response.status_code == 200
    assert jwt.decode(response.json()['access_token'], auth._jwt_secret(), algorithms=['HS256'])['ver'] == 1


@pytest.mark.parametrize('setting,value', [('AUTH_DISABLED', 'true'), ('JWT_SECRET_KEY', 'short'),
                                         ('RATE_LIMIT_ENABLED', 'false'), ('SECURITY_REDIS_URL', 'rediss://localhost?ssl_cert_reqs=none')])
def test_production_guards(monkeypatch, setting, value):
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('RATE_LIMIT_ENABLED', 'true')
    monkeypatch.setenv('SECURITY_REDIS_URL', 'redis://localhost:6379/0')
    monkeypatch.setenv(setting, value)
    with pytest.raises(AppError):
        validate_security_settings()


def test_production_bootstrap_and_bypass_disabled_even_before_startup(monkeypatch):
    monkeypatch.setenv('RENDER', 'true')
    monkeypatch.setenv('BOOTSTRAP_ADMIN_TOKEN', 'bootstrap')
    monkeypatch.setenv('AUTH_DISABLED', 'true')
    assert not auth._auth_disabled()
    assert not auth._bootstrap_admin_token()


def test_redaction_covers_errors_logs_and_durable_failures(monkeypatch):
    secret = 'canary-secret-never-disclose'
    monkeypatch.setenv('GEMINI_API_KEY', secret)
    diagnostic = f'upstream {secret} https://private.example/?key=secret /tmp/private.txt Bearer opaque-credential'
    error = AppError(diagnostic, code='storage_error', details={'password': 'plaintext', 'nested': [diagnostic]})
    response = error_response(500, error.code, error.message, error.details)
    record = logging.LogRecord('test', logging.ERROR, __file__, 1, diagnostic, (), None)
    record.refresh_token = 'plaintext'
    for text in (response.body.decode(), JsonLogFormatter().format(record), safe_failure(error), safe_failure(RuntimeError(diagnostic))):
        for forbidden in (secret, 'private.example', '/tmp/private.txt', 'opaque-credential', 'plaintext'):
            assert forbidden not in text
    assert safe_failure(AppError('Safe message', code='error', internal_message=diagnostic)) == 'Safe message'


def test_request_model_limits_and_no_password_echo():
    client = TestClient(create_app())
    response = client.post('/auth/login', json={'email': 'a@example.org', 'password': 'x' * 257})
    assert response.status_code == 422
    assert 'x' * 257 not in response.text
    from pydantic import ValidationError

    from schemas import AskRequest
    with pytest.raises(ValidationError):
        AskRequest(question='x' * 8001)


def test_declared_body_size_rejected_before_routing(monkeypatch):
    monkeypatch.setenv('AUTH_REQUEST_MAX_BYTES', '32')
    response = TestClient(create_app()).post('/auth/login', content=b'x' * 33)
    assert response.status_code == 413


def test_chunked_body_and_disconnect_release_slot(monkeypatch):
    closed = []
    called = []
    monkeypatch.setenv('REQUEST_MAX_BYTES', '4')
    monkeypatch.setattr(security_middleware, 'acquire', lambda *a, **kw: SimpleNamespace(close=lambda: closed.append(True)))
    async def downstream(scope, receive, send):
        called.append(True)
    async def run(messages):
        sent = []
        async def receive():
            return messages.pop(0)
        async def send(message):
            sent.append(message)
        await security_middleware.SecurityMiddleware(downstream)(
            {'type': 'http', 'method': 'POST', 'path': '/ask', 'headers': [], 'client': ('127.0.0.1', 1)}, receive, send)
        return sent
    sent = asyncio.run(run([{'type': 'http.request', 'body': b'abc', 'more_body': True},
                            {'type': 'http.request', 'body': b'de', 'more_body': False}]))
    assert sent[0]['status'] == 413
    assert not called
    assert len(closed) == 1
    assert asyncio.run(run([{'type': 'http.disconnect'}])) == []
    assert len(closed) == 2


def test_redis_outage_fails_closed(monkeypatch):
    monkeypatch.setenv('RATE_LIMIT_ENABLED', 'true')
    def fail():
        raise ConnectionError('secret backend error')
    monkeypatch.setattr(request_limits, 'client', fail)
    with pytest.raises(AppError) as error:
        request_limits.acquire('generation', 'user')
    assert error.value.status_code == 503
    assert error.value.headers['Retry-After'] == '5'
    assert 'secret backend' not in error.value.message


def test_worker_capacity_retry_and_release(monkeypatch):
    from celery.exceptions import Retry

    monkeypatch.setenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    import tasks
    retries = []
    def retry(**kwargs):
        retries.append(kwargs)
        return Retry()
    task = SimpleNamespace(retry=retry)
    def reject(_job):
        raise AppError('Busy', code='rate_limited', status_code=429, headers={'Retry-After': '7'})
    monkeypatch.setattr(tasks, 'acquire_worker', reject)
    with pytest.raises(Retry), tasks.worker_slot(task, 'job'):
        pytest.fail('Rejected job must not execute')
    assert retries == [{'countdown': 7, 'max_retries': None}]
    assert tasks.run_ingest_job_task.max_retries is None
    closed = []
    monkeypatch.setattr(tasks, 'acquire_worker', lambda _: SimpleNamespace(close=lambda: closed.append(True)))
    with pytest.raises(ValueError), tasks.worker_slot(task, 'job'):
        raise ValueError('Execution failed')
    assert closed == [True]


def test_user_quota_is_wired_after_authentication(monkeypatch):
    app = create_app()
    app.dependency_overrides[auth.get_current_user] = lambda: auth.CurrentUser('real-user', 'a@example.org', 'viewer')
    admissions = []
    def acquire(bucket, identity, **kwargs):
        admissions.append((bucket, identity, kwargs))
    monkeypatch.setattr(request_limits, 'acquire', acquire)
    assert TestClient(app).get('/auth/me').status_code == 200
    assert ('api', 'real-user', {}) in admissions


def test_placeholder_secret_and_excessive_request_target_rejected(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('JWT_SECRET_KEY', 'replace_with_long_random_jwt_secret')
    with pytest.raises(AppError):
        validate_security_settings()
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.setenv('REQUEST_TARGET_MAX_BYTES', '20')
    response = TestClient(create_app()).get('/auth/me?value=' + 'x' * 30)
    assert response.status_code == 414
