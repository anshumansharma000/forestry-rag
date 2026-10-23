"""Versioned exact caches. Optional cache outages bypass reuse, never authorization."""

import copy
import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from uuid import uuid4

import redis

from pipeline_settings import integer, policy_digest
from pipeline_trace import event, measured

_pools = {}
_pool_lock = threading.Lock()

_access: ContextVar[str | None] = ContextVar("cache_access", default=None)
_RELEASE = "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0"
_PUBLISH = """
if redis.call('GET',KEYS[1])~=ARGV[1] then return 0 end
local now=tonumber(redis.call('TIME')[1])
redis.call('ZREMRANGEBYSCORE',KEYS[3],'-inf',now)
if redis.call('ZCARD',KEYS[3])>=tonumber(ARGV[4]) then
  local victim=redis.call('ZRANGE',KEYS[3],0,0)[1]
  if victim then redis.call('DEL',victim); redis.call('ZREM',KEYS[3],victim) end
end
redis.call('SET',KEYS[2],ARGV[2],'EX',ARGV[3])
redis.call('ZADD',KEYS[3],now+tonumber(ARGV[3]),KEYS[2])
redis.call('EXPIRE',KEYS[3],86401)
redis.call('DEL',KEYS[1])
return 1
"""


@contextmanager
def access_scope(user_id):
    token = _access.set(str(user_id) if user_id else None)
    try:
        yield
    finally:
        _access.reset(token)


def current_access():
    return _access.get()


def secret():
    return os.getenv("RAG_CACHE_HMAC_KEY", "").strip()


def opaque(value):
    return hmac.new(
        secret().encode(), json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode(), hashlib.sha256
    ).hexdigest()


@dataclass(frozen=True)
class RetrievalScope:
    kind: str
    backend: str
    access: str
    revision: str = ""
    namespace: str = ""
    date: str = ""
    policy: str = ""

    @classmethod
    def build(cls, repository, namespace=""):
        from datetime import date

        from query_context import current_context

        context = current_context()
        revision = str(getattr(repository, "revision_id", "") or "")
        return cls(
            "lab" if revision else ("legal" if namespace else "corpus"),
            opaque([os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")]),
            opaque(current_access()) if current_access() else "",
            revision,
            namespace,
            context.today if context else date.today().isoformat(),
            policy_digest(),
        )


def cache_key(layer, material):
    return "answer-cache:{pipeline}:" + layer + ":" + opaque(material)


class RedisCache:
    """Short-lived client; bounded keyspace; every lock owner is fenced."""

    def __init__(self, connection):
        self.connection = connection

    def get(self, key):
        value = self.connection.get(key)
        if value is None:
            return None
        if len(value.encode()) > integer("RAG_CACHE_MAX_VALUE_BYTES", 524288, 1024, 4194304):
            return None
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or decoded.get("version") != 1 or "value" not in decoded:
            return None
        return decoded["value"]

    def acquire(self, key, owner):
        return bool(self.connection.set(key + ":lock", owner, nx=True, ex=integer("RAG_CACHE_LOCK_SECONDS", 60, 1, 300)))

    def publish(self, key, owner, value, ttl):
        encoded = json.dumps({"version": 1, "value": value}, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > integer("RAG_CACHE_MAX_VALUE_BYTES", 524288, 1024, 4194304):
            return False
        return bool(
            self.connection.eval(
                _PUBLISH,
                3,
                key + ":lock",
                key,
                "answer-cache:{pipeline}:entries",
                owner,
                encoded,
                ttl,
                integer("RAG_CACHE_MAX_ENTRIES", 512, 1, 10000),
            )
        )

    def release(self, key, owner):
        self.connection.eval(_RELEASE, 1, key + ":lock", owner)

    def close(self):
        self.connection.close()


def backend():
    url = os.getenv("RAG_CACHE_REDIS_URL", "").strip()
    if not url or len(secret().encode()) < 32:
        return None
    with _pool_lock:
        if url not in _pools:
            _pools[url] = redis.ConnectionPool.from_url(
                url, socket_connect_timeout=0.3, socket_timeout=0.3, max_connections=16, decode_responses=True
            )
        pool = _pools[url]
    return RedisCache(redis.Redis(connection_pool=pool))


@measured("cache")
def cached_compute(layer, material, compute, *, enabled, validate, fresh=lambda: True, ttl=300):
    if not enabled:
        return compute()
    key = cache_key(layer, material)
    owner = uuid4().hex
    try:
        store = backend()
    except (redis.RedisError, ValueError, TypeError):
        event("cache_bypass", layer=layer, reason="cache_unavailable")
        return compute()
    if store is None:
        event("cache_bypass", layer=layer, reason="unconfigured")
        return compute()
    owned = False
    try:
        try:
            value = store.get(key)
            if value is not None and validate(value) and fresh():
                event("cache_hit", layer=layer)
                return copy.deepcopy(value)
            owned = store.acquire(key, owner)
            if not owned:
                until = time.monotonic() + integer("RAG_CACHE_WAIT_MS", 100, 0, 3000) / 1000
                while time.monotonic() < until:
                    time.sleep(0.02)
                    value = store.get(key)
                    if value is not None and validate(value) and fresh():
                        event("cache_hit", layer=layer, coalesced=True)
                        return copy.deepcopy(value)
                event("cache_bypass", layer=layer, reason="leader_wait_expired")
        except (redis.RedisError, ValueError, TypeError, KeyError):
            event("cache_bypass", layer=layer, reason="cache_unavailable")
        # Never catch compute failures and repeat a paid call as a cache fallback.
        value = compute()
        if owned:
            try:
                if validate(value) and fresh():
                    store.publish(key, owner, value, ttl)
                    event("cache_miss", layer=layer)
            except (redis.RedisError, ValueError, TypeError, KeyError):
                event("cache_bypass", layer=layer, reason="publication_unavailable")
        return value
    finally:
        try:
            if owned:
                store.release(key, owner)
            store.close()
        except redis.RedisError:
            pass


def valid_embedding(value, dimensions):
    return (
        isinstance(value, list)
        and len(value) == dimensions
        and all(type(v) in (float, int) and math.isfinite(v) for v in value)
        and abs(sum(v * v for v in value) - 1.0) <= 1e-5
    )


def valid_contexts(value):
    if not isinstance(value, list) or not 0 < len(value) <= 20:
        return False
    return all(
        isinstance(v, dict)
        and all(k in v for k in ("id", "document_id", "source", "chunk_index", "text", "score", "metadata"))
        and isinstance(v["text"], str)
        and bool(v["text"])
        and isinstance(v["metadata"], dict)
        and type(v["score"]) in (int, float)
        and math.isfinite(v["score"])
        for v in value
    )


def versions(repository, scope):
    if not hasattr(repository, "cache_versions"):
        return None
    try:
        value = repository.cache_versions(scope.namespace, scope.revision)
        return value if isinstance(value, dict) and type(value.get("corpus")) is int and type(value.get("registry")) is int else None
    except Exception:
        # A missing capability or storage failure disables reuse, never invents a version.
        event("cache_bypass", layer="versions", reason="version_unavailable")
        return None


def retrieval_cached(repository, question, top_k, options, compute, *, namespace=""):
    from pipeline_settings import PipelineSettings

    policy = PipelineSettings.read()
    if not policy.retrieval_cache or not current_access():
        return compute()
    scope = RetrievalScope.build(repository, namespace)
    version = versions(repository, scope)
    if version is None:
        return compute()
    from query_context import current_context

    context = current_context()
    material = [asdict(scope), version, question, top_k, options, asdict(context) if context else None]
    return cached_compute(
        "retrieval",
        material,
        compute,
        enabled=True,
        validate=valid_contexts,
        fresh=lambda: versions(repository, scope) == version,
        ttl=integer("RAG_RETRIEVAL_CACHE_TTL_SECONDS", 300, 1, 300),
    )


def request_access(parameter):
    """Run only after FastAPI's authentication dependency has supplied the user."""
    from functools import wraps
    from inspect import signature

    def decorator(function):
        sig = signature(function)

        @wraps(function)
        def wrapped(*args, **kwargs):
            user = sig.bind_partial(*args, **kwargs).arguments.get(parameter)
            identity = [str(user.id), user.role] if user is not None and hasattr(user, "id") else None
            with access_scope(identity):
                return function(*args, **kwargs)

        return wrapped

    return decorator


def answer_cached(question, top_k, compute):
    """Narrow standalone source-fact reuse, after auth, evidence and semantic audit."""
    from evidence_policy import RISK
    from pipeline_settings import PipelineSettings
    from pipeline_trace import current_trace
    from prompts import answer_outcome, validate_answer_citations
    from repositories import DocumentRepository
    from retrieval import is_narrow_lookup
    from schemas import AskResponse
    from temporal import historical_question

    if (
        not PipelineSettings.read().answer_cache
        or not current_access()
        or RISK.search(question)
        or historical_question(question)
        or not (
            is_narrow_lookup(question)
            or re.fullmatch(
                r"what (?:is|are) (?:the )?(?:permit |application |transit )?" r"(?:fee|fees|deadline|duration|validity|form)\??",
                question.strip(),
                re.I,
            )
        )
    ):
        return compute()
    repository = DocumentRepository()
    namespace = ""
    from settings import env_bool

    if env_bool("RAG_LEGAL_HIERARCHY"):
        from legal_registry import legal_namespace

        namespace = legal_namespace()
    scope = RetrievalScope.build(repository, namespace)
    version = versions(repository, scope)
    if version is None:
        return compute()

    def validate(value):
        if not isinstance(value, dict) or value.get("approval") not in ("verify", "verify_complex", "jev"):
            return False
        try:
            payload = value.get("response")
            if not isinstance(payload, dict) or not {"answer", "outcome", "sources", "cited_sources", "abstained"} <= payload.keys():
                return False
            if not payload.keys() <= AskResponse.model_fields.keys():
                return False
            result = AskResponse.model_validate(payload).model_dump()
            sources = result["sources"]
            if not sources or not all(math.isfinite(s["score"]) for s in sources):
                return False
            answer = result["answer"]
            if [s["citation_number"] for s in sources] != list(range(1, len(sources) + 1)):
                return False
            numbers = {int(n) for group in re.findall(r"\[((?:\d+\s*,\s*)*\d+)\]", answer) for n in re.findall(r"\d+", group)}
            if result["cited_sources"] != [s for s in sources if s["citation_number"] in numbers]:
                return False
            return (
                result["outcome"] == "answered"
                and result["answer_origin"] == "documents"
                and not result["web_sources"]
                and not result["abstained"]
                and answer_outcome(answer) == "answered"
                and validate_answer_citations(answer, len(sources), require_citation=True) == answer
            )
        except (ValueError, TypeError, KeyError):
            return False

    def produce():
        trace = current_trace()
        if trace is not None:
            trace.final_audit = None
        response = compute()
        return {"response": response, "approval": trace.final_audit if trace else None}

    value = cached_compute(
        "answer",
        [asdict(scope), version, question, top_k],
        produce,
        enabled=True,
        validate=validate,
        fresh=lambda: versions(repository, scope) == version,
        ttl=integer("RAG_ANSWER_CACHE_TTL_SECONDS", 300, 1, 300),
    )
    return value["response"]


def close_cache_connections():
    with _pool_lock:
        for pool in _pools.values():
            pool.disconnect()
        _pools.clear()
