"""Bounded ordered search concurrency and opt-in thread-owned HTTP sessions."""

import atexit
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import requests

from pipeline_settings import PipelineSettings
from pipeline_trace import measured
from settings import env_bool

_pool = None
_pool_lock = threading.Lock()


def search_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="answer-search")
        return _pool


def shutdown_searches():
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=True)


atexit.register(shutdown_searches)
_local = threading.local()
_sessions = []
_session_epoch = 0
_session_lock = threading.Lock()


@measured("retrieval_searches")
def ordered_search(items, function, *, safe=False):
    if not safe or PipelineSettings.read().concurrency == 1 or len(items) < 2:
        return [function(item) for item in items]
    # At most two submitted branches per request, four active across this process.
    results = []
    for start in range(0, len(items), 2):
        futures = [search_pool().submit(copy_context().run, function, item) for item in items[start : start + 2]]
        error = None
        batch = []
        for future in futures:
            try:
                batch.append(future.result())
            except Exception as exc:
                error = error or exc
        # Join every started worker before returning/error so request accounting closes last.
        if error is not None:
            raise error
        results.extend(batch)
    return results


def post(*args, **kwargs):
    if not env_bool("RAG_HTTP_POOLING"):
        return requests.post(*args, **kwargs)
    pid = os.getpid()
    if getattr(_local, "pid", None) != (pid, _session_epoch):
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        session.mount("https://", adapter)
        _local.session, _local.pid = session, (pid, _session_epoch)
        with _session_lock:
            _sessions.append(session)
    return _local.session.post(*args, **kwargs)


def close_sessions():
    global _session_epoch
    with _session_lock:
        for session in _sessions:
            session.close()
        _sessions.clear()
        _session_epoch += 1
    if hasattr(_local, "pid"):
        del _local.pid


atexit.register(close_sessions)
