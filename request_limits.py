"""Atomic Redis sliding-window quotas and renewable distributed concurrency leases."""

import hashlib
import logging
import re
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from uuid import uuid4

import redis
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from errors import AppError, ErrorCode
from security_settings import limits_enabled, positive_int, redis_url

logger = logging.getLogger(__name__)

# Every key shares one Redis Cluster hash slot. Redis TIME avoids replica clock drift.
ACQUIRE = """
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)
local nr = tonumber(ARGV[2])
local lease = tonumber(ARGV[3])
for i,key in ipairs(KEYS) do
  local window = i <= nr and 60000 or lease
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
  if redis.call('ZCARD', key) >= tonumber(ARGV[3+i]) then
    local first = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    return {0, math.max(1, math.ceil((tonumber(first[2]) + window - now) / 1000))}
  end
end
for i,key in ipairs(KEYS) do
  redis.call('ZADD', key, now, ARGV[1])
  redis.call('PEXPIRE', key, i <= nr and 60000 or lease)
end
return {1, 0}
"""
RENEW = """
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)
for _,key in ipairs(KEYS) do
  if not redis.call('ZSCORE', key, ARGV[1]) then return 0 end
end
for _,key in ipairs(KEYS) do
  redis.call('ZADD', key, 'XX', now, ARGV[1])
  redis.call('PEXPIRE', key, ARGV[2])
end
return 1
"""
RELEASE = "for _,key in ipairs(KEYS) do redis.call('ZREM', key, ARGV[1]) end return 1"

# Defaults are per minute. Global limits refer to all API replicas together.
DEFAULTS = {
    "api": (600, 120, 0, 0),
    "auth": (20, 10, 4, 2),
    "generation": (120, 20, 8, 2),
    "upload": (60, 20, 4, 1),
    "job": (60, 10, 4, 1),
}


def bucket_for(method: str, path: str) -> str:
    if method != "POST":
        return "api"
    if path in {"/auth/login", "/auth/refresh", "/auth/change-password", "/admin/users"} or path.endswith("/reset-password"):
        return "auth"
    if (
        path == "/admin/legal/preview"
        or path == "/ask"
        or re.fullmatch(r"/chat/sessions/[^/]+/ask", path)
        or re.fullmatch(r"/admin/rag-lab/revisions/[^/]+/query", path)
    ):
        return "generation"
    if path.startswith("/documents/upload") or re.fullmatch(r"/admin/rag-lab/experiments/[^/]+/files", path):
        return "upload"
    if path == "/ingest" or re.fullmatch(r"/admin/rag-lab/(experiments/[^/]+/revisions|revisions/[^/]+/publish)", path):
        return "job"
    return "api"


@lru_cache(maxsize=4)
def redis_client(url: str):
    options = {"ssl_cert_reqs": "required"} if url.startswith("rediss://") else {}
    return redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True, **options)


def client():
    url = redis_url()
    if not url:
        raise AppError("Request protection is unavailable.", code=ErrorCode.CONFIG_ERROR, status_code=503)
    return redis_client(url)


def check_available() -> None:
    if not limits_enabled():
        return
    try:
        client().ping()
    except Exception as exc:
        raise AppError("Request protection is unavailable.", code=ErrorCode.STORAGE_ERROR, status_code=503) from exc


def key(kind: str, bucket: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"{{rag-security}}:{kind}:{bucket}:{digest}"


@dataclass
class Lease:
    connection: object
    keys: list[str]
    member: str
    seconds: int

    def __post_init__(self):
        self.stop = threading.Event()
        self.thread = None
        if self.keys:
            self.thread = threading.Thread(target=self._renew, daemon=True, name="request-limit-lease")
            self.thread.start()

    def _renew(self):
        while not self.stop.wait(self.seconds / 3):
            try:
                renewed = self.connection.eval(RENEW, len(self.keys), *self.keys, self.member, self.seconds * 1000)
                if not renewed:
                    logger.error("request_limit_lease_lost")
                    return
            except Exception:
                # New admissions fail closed; crashed/lost leases eventually expire.
                logger.error("request_limit_lease_renewal_failed")

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)
        if self.keys:
            try:
                self.connection.eval(RELEASE, len(self.keys), *self.keys, self.member)
            except Exception:
                logger.error("request_limit_release_failed")


def acquire(bucket: str, identity: str, *, by_ip: bool = False) -> Lease | None:
    if not limits_enabled():
        return None
    ip_rate, user_rate, global_concurrency, user_concurrency = DEFAULTS[bucket]
    rate = positive_int(f'LIMIT_{bucket.upper()}_{"IP" if by_ip else "USER"}_PER_MINUTE', ip_rate if by_ip else user_rate)
    rate_keys = [key("rate-ip" if by_ip else "rate-user", bucket, identity)]
    capacities = [rate]
    concurrency_keys = []
    # IP admissions protect multipart parsing and password hashing before authentication.
    # Generation/job slots are acquired by the authenticated dependency.
    use_concurrency = (by_ip and bucket in {"auth", "upload"}) or (not by_ip and bucket in {"generation", "job"})
    if use_concurrency:
        concurrency_keys = [key("concurrent", bucket, "global"), key("concurrent", bucket, ("ip:" if by_ip else "user:") + identity)]
        capacities.extend(
            [
                positive_int(f"LIMIT_{bucket.upper()}_GLOBAL_CONCURRENCY", global_concurrency),
                positive_int(f'LIMIT_{bucket.upper()}_{"IP" if by_ip else "USER"}_CONCURRENCY', user_concurrency),
            ]
        )
    keys = rate_keys + concurrency_keys
    seconds = positive_int("LIMIT_LEASE_SECONDS", 120)
    member = uuid4().hex
    try:
        connection = client()
        allowed, retry = connection.eval(ACQUIRE, len(keys), *keys, member, len(rate_keys), seconds * 1000, *capacities)
    except Exception as exc:
        raise AppError(
            "Request protection is temporarily unavailable. Please retry.",
            code=ErrorCode.STORAGE_ERROR,
            status_code=503,
            headers={"Retry-After": "5"},
        ) from exc
    if not allowed:
        raise AppError(
            "Too many requests. Please retry later.",
            code=ErrorCode.RATE_LIMITED,
            status_code=429,
            details={"retry_after_seconds": retry},
            headers={"Retry-After": str(retry)},
        )
    return Lease(connection, concurrency_keys, member, seconds)


@asynccontextmanager
async def user_limits(request: Request, identity: str):
    lease = await run_in_threadpool(acquire, bucket_for(request.method, request.url.path.rstrip("/")), identity)
    try:
        yield
    finally:
        if lease:
            await run_in_threadpool(lease.close)


def acquire_worker(job_id: str) -> Lease | None:
    """Hold a slot for the whole task, shared by ingestion and lab workers."""
    if not limits_enabled():
        return None
    keys = [key("concurrent", "worker", "global"), key("concurrent", "worker", job_id)]
    seconds = positive_int("LIMIT_LEASE_SECONDS", 120)
    member = uuid4().hex
    try:
        connection = client()
        allowed, retry = connection.eval(
            ACQUIRE, len(keys), *keys, member, 0, seconds * 1000, positive_int("LIMIT_WORKER_GLOBAL_CONCURRENCY", 2), 1
        )
    except Exception as exc:
        raise AppError(
            "Worker protection is temporarily unavailable.", code=ErrorCode.STORAGE_ERROR, status_code=503, headers={"Retry-After": "5"}
        ) from exc
    if not allowed:
        raise AppError("Worker capacity is busy.", code=ErrorCode.RATE_LIMITED, status_code=429, headers={"Retry-After": str(retry)})
    return Lease(connection, keys, member, seconds)
