"""Atomic budget tests against the isolated Redis fixture, never live services."""

import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import web_limits
from tests.test_security_redis import connection  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RAG_REDIS_TESTS") != "1", reason="opt-in isolated Redis tests")


@pytest.fixture
def store(connection, monkeypatch):  # noqa: F811
    monkeypatch.setattr(web_limits, "client", lambda: connection)
    return connection


def result(queries=1):
    return SimpleNamespace(model="gemini-3.5-flash-lite", grounding_metadata={"webSearchQueries": [str(i) for i in range(queries)]},
                           usage_metadata={"promptTokenCount": 1000, "candidatesTokenCount": 1000, "thoughtsTokenCount": 0})


def test_parallel_reservations_cannot_exceed_user_quota(store, monkeypatch):
    monkeypatch.setenv("RAG_WEB_USER_DAILY_CALLS", "3")
    with ThreadPoolExecutor(max_workers=12) as pool:
        reservations = list(pool.map(lambda _: web_limits.reserve_web_search("private-user@example.com"), range(20)))
    accepted = [r for r in reservations if r is not None]
    assert len(accepted) == 3
    assert not any("private-user" in k for k in store.keys("*"))
    for r in accepted:
        r.settle(result())
    assert web_limits.reserve_web_search("private-user@example.com") is None
    assert web_limits.reserve_web_search("another-user") is not None


@pytest.mark.parametrize("setting,value", [("RAG_WEB_GLOBAL_DAILY_CALLS", "1"), ("RAG_WEB_MONTHLY_QUERIES", "3"),
                                           ("RAG_WEB_DAILY_BUDGET_USD", ".10")])
def test_global_quotas_reserve_before_provider_work(store, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    assert web_limits.reserve_web_search("one") is not None
    assert web_limits.reserve_web_search("two") is None


def test_settlement_reconciles_actual_cost_once(store):
    reservation = web_limits.reserve_web_search("user")
    assert int(store.get(reservation.keys[2])) == 3
    reservation.settle(result(2))
    assert int(store.get(reservation.keys[2])) == 2
    assert int(store.get(reservation.keys[3])) == 30800
    reservation.settle(result(2))
    assert int(store.get(reservation.keys[2])) == 2
    assert int(store.get(reservation.keys[3])) == 30800


def test_actual_queries_beyond_reservation_block_future_work(store, monkeypatch):
    monkeypatch.setenv("RAG_WEB_MONTHLY_QUERIES", "4")
    reservation = web_limits.reserve_web_search("user")
    reservation.settle(result(6))
    assert int(store.get(reservation.keys[2])) == 6
    assert web_limits.reserve_web_search("other") is None


def test_unknown_cost_retains_reservation_and_latches_circuit(store):
    reservation = web_limits.reserve_web_search("user")
    reservation.settle(None)
    assert int(store.get(reservation.keys[2])) == 3
    assert int(store.get(reservation.keys[3])) == 100000
    assert store.get(reservation.keys[4]) == "unknown-cost"
    assert web_limits.reserve_web_search("other") is None


def test_monthly_bucket_shared_across_days_and_daily_limits_reset(store, monkeypatch):
    monkeypatch.setattr(store, "time", lambda: (1792670400, 0))  # 22 October 2026 UTC
    first = web_limits.reserve_web_search("user")
    first.settle(result())
    monkeypatch.setattr(store, "time", lambda: (1792756800, 0))
    second = web_limits.reserve_web_search("user")
    assert first.keys[0] != second.keys[0]
    assert first.keys[2] == second.keys[2]
    assert int(store.get(second.keys[2])) == 4
