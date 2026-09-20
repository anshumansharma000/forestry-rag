"""Real Redis tests using an isolated Unix socket, never the configured Redis service."""
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis

import request_limits as limits
from errors import AppError

pytestmark = pytest.mark.skipif(os.getenv('RAG_REDIS_TESTS') != '1', reason='opt-in isolated Redis tests')


@pytest.fixture
def connection(tmp_path, monkeypatch):
    executable = shutil.which('redis-server')
    if not executable:
        pytest.fail('redis-server is required for RAG_REDIS_TESTS=1')
    # macOS Unix socket paths must be short.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='rag-redis-', dir='/tmp') as directory:
        socket = directory + '/redis.sock'
        process = subprocess.Popen([executable, '--port', '0', '--unixsocket', socket, '--save', '', '--appendonly', 'no'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        connection = redis.Redis(unix_socket_path=socket, decode_responses=True)
        try:
            for _ in range(100):
                try:
                    connection.ping()
                    break
                except redis.ConnectionError:
                    if process.poll() is not None:
                        pytest.fail(process.stderr.read().decode())
                    time.sleep(.02)
            else:
                pytest.fail('Isolated Redis did not become ready')
            monkeypatch.setenv('RATE_LIMIT_ENABLED', 'true')
            monkeypatch.setattr(limits, 'client', lambda: connection)
            yield connection
        finally:
            connection.close()
            process.terminate()
            process.wait(timeout=5)


def test_atomic_admission_across_clients_and_release(connection, monkeypatch):
    monkeypatch.setenv('LIMIT_GENERATION_GLOBAL_CONCURRENCY', '3')
    monkeypatch.setenv('LIMIT_GENERATION_USER_CONCURRENCY', '2')
    def enter(i):
        try:
            # Each thread uses its own connection pool, like separate API processes.
            local = redis.Redis(connection_pool=redis.ConnectionPool(
                connection_class=redis.UnixDomainSocketConnection,
                path=connection.connection_pool.connection_kwargs['path'], decode_responses=True))
            keys = [limits.key('concurrent', 'generation', 'global')]
            return local.eval(limits.ACQUIRE, 1, *keys, str(i), 0, 120000, 3)
        finally:
            local.close()
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(enter, range(20)))
    assert sum(result[0] for result in results) == 3
    connection.flushdb()
    first = limits.acquire('generation', 'user')
    second = limits.acquire('generation', 'user')
    try:
        with pytest.raises(AppError) as rejected:
            limits.acquire('generation', 'user')
        assert rejected.value.status_code == 429
        first.close()
        third = limits.acquire('generation', 'user')
        third.close()
    finally:
        first.close()
        second.close()
    assert connection.zcard(limits.key('concurrent', 'generation', 'global')) == 0


def test_quota_survives_lease_release_and_has_retry_after(connection, monkeypatch):
    monkeypatch.setenv('LIMIT_API_USER_PER_MINUTE', '2')
    for _ in range(2):
        limits.acquire('api', 'user').close()
    with pytest.raises(AppError) as rejected:
        limits.acquire('api', 'user')
    assert rejected.value.status_code == 429
    assert 1 <= int(rejected.value.headers['Retry-After']) <= 60
    # Independent identities are allowed and raw identity data is absent from keys.
    limits.acquire('api', 'another@example.org').close()
    assert not any('another@example.org' in key for key in connection.keys('*'))


def test_live_lease_renews_and_crashed_lease_expires(connection, monkeypatch):
    monkeypatch.setenv('LIMIT_LEASE_SECONDS', '1')  # Faster integration test; production validates >=10.
    first = limits.acquire_worker('job-1')
    try:
        time.sleep(1.3)
        assert connection.zscore(first.keys[0], first.member) is not None
        with pytest.raises(AppError):
            limits.acquire_worker('job-1')
        first.stop.set()  # Simulate a crashed process without release.
        first.thread.join()
        time.sleep(1.1)
        replacement = limits.acquire_worker('job-1')
        replacement.close()
    finally:
        first.close()
