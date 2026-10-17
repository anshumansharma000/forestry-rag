"""Bounded request-local diagnostics; events contain codes/counts, never source text."""

import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps

logger = logging.getLogger(__name__)


@dataclass
class Trace:
    events: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    final_audit: str | None = None

    def add(self, event: dict):
        with self.lock:
            if len(self.events) < 500:
                self.events.append(event)


_current: ContextVar[Trace | None] = ContextVar("pipeline_trace", default=None)


def current_trace():
    return _current.get()


def event(code: str, **metrics):
    trace = _current.get()
    if trace is not None:
        trace.add({"code": code, **metrics})


def audited(owner: str, supported: bool):
    trace = _current.get()
    if trace is not None:
        trace.final_audit = owner if supported else None
    event("verification_result", owner=owner, supported=supported)


@contextmanager
def trace_request():
    if _current.get() is not None:
        yield _current.get()
        return
    trace = Trace()
    token = _current.set(trace)
    try:
        yield trace
    finally:
        _current.reset(token)


@contextmanager
def stage(name: str):
    started = time.perf_counter()
    succeeded = False
    try:
        yield
        succeeded = True
    finally:
        event("stage", stage=name, duration_ms=round((time.perf_counter() - started) * 1000, 3), succeeded=succeeded)


def measured(name: str):
    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with stage(name):
                return function(*args, **kwargs)

        return wrapped

    return decorator
