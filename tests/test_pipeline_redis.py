"""Real Redis lock fencing/publication tests on the isolated socket fixture."""

import os
import time

import pytest

from query_cache import RedisCache
from tests.test_security_redis import connection  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RAG_REDIS_TESTS") != "1", reason="opt-in isolated Redis tests")


def test_cache_lock_owner_cannot_publish_or_release_another_owner(connection):  # noqa: F811
    store = RedisCache(connection)
    key = "answer-cache:{pipeline}:test"
    assert store.acquire(key, "first")
    assert not store.acquire(key, "second")
    assert not store.publish(key, "second", {"answer": 2}, 10)
    store.release(key, "second")
    assert connection.get(key + ":lock") == "first"
    assert store.publish(key, "first", {"answer": 1}, 10)
    assert store.get(key) == {"answer": 1}
    assert not connection.exists(key + ":lock")


def test_expired_leader_cannot_overwrite_new_owner(connection):  # noqa: F811
    store = RedisCache(connection)
    key = "answer-cache:{pipeline}:expired"
    connection.set(key + ":lock", "old", px=20)
    time.sleep(0.04)
    assert store.acquire(key, "new")
    assert not store.publish(key, "old", [1], 10)
    assert store.publish(key, "new", [2], 10)
    assert store.get(key) == [2]


def test_keyspace_and_payload_bounds(connection, monkeypatch):  # noqa: F811
    store = RedisCache(connection)
    monkeypatch.setenv("RAG_CACHE_MAX_ENTRIES", "1")
    for name in ("one", "two"):
        assert store.acquire(name, "owner")
    assert store.publish("one", "owner", [1], 10)
    assert store.publish("two", "owner", [2], 10)
    assert store.get("one") is None
    assert store.get("two") == [2]
    store.release("two", "owner")
    monkeypatch.setenv("RAG_CACHE_MAX_VALUE_BYTES", "1024")
    assert store.acquire("big", "owner")
    assert not store.publish("big", "owner", "x" * 2000, 10)


def test_controlled_io_benchmark_has_real_redis_reuse(connection):  # noqa: F811
    import json
    from pathlib import Path

    from scripts.benchmark_pipeline_io import run_benchmark

    report = run_benchmark(connection)
    assert report["results"]["uncached"]["simulated_upstream_calls"] == 8
    assert report["results"]["cold"]["simulated_upstream_calls"] == 8
    assert report["results"]["warm"]["simulated_upstream_calls"] == 0
    assert report["results"]["coalesced"]["simulated_upstream_calls"] == 1
    output = os.getenv("RAG_BENCHMARK_OUTPUT")
    if output:
        Path(output).write_text(json.dumps(report, indent=2) + "\n")
