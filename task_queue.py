import logging
import os
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import uuid4

from celery.exceptions import CeleryError
from fastapi import status
from kombu.exceptions import OperationalError

from errors import AppError, ErrorCode
from settings import env_bool, env_int

logger = logging.getLogger(__name__)


def celery_broker_url() -> str:
    return normalize_redis_ssl_url((os.getenv("CELERY_BROKER_URL") or os.getenv("REDIS_URL") or "").strip())


def celery_result_backend() -> str | None:
    value = os.getenv("CELERY_RESULT_BACKEND", "").strip()
    return normalize_redis_ssl_url(value) if value else None


def normalize_redis_ssl_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "rediss":
        return url

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "ssl_cert_reqs" in query:
        return url

    query["ssl_cert_reqs"] = "required"
    return urlunparse(parsed._replace(query=urlencode(query)))


def celery_visibility_timeout_seconds() -> int:
    return env_int("CELERY_VISIBILITY_TIMEOUT_SECONDS", 3600)


def celery_task_max_retries() -> int:
    return env_int("CELERY_TASK_MAX_RETRIES", 3)


def celery_task_retry_base_seconds() -> int:
    return env_int("CELERY_TASK_RETRY_BASE_SECONDS", 60)


def celery_worker_status() -> dict:
    if not celery_broker_url():
        return {
            "status": "unavailable",
            "broker_configured": False,
            "broker_reachable": False,
            "workers_online": 0,
        }

    from tasks import celery_app

    try:
        replies = celery_app.control.ping(timeout=env_int("CELERY_WORKER_PING_TIMEOUT_SECONDS", 2)) or []
    except Exception:
        logger.exception("celery_worker_ping_failed")
        return {
            "status": "unavailable",
            "broker_configured": True,
            "broker_reachable": False,
            "workers_online": 0,
        }

    workers_online = sum(1 for reply in replies if isinstance(reply, dict) and any(value == {"ok": "pong"} for value in reply.values()))
    return {
        "status": "ok" if workers_online else "unavailable",
        "broker_configured": True,
        "broker_reachable": True,
        "workers_online": workers_online,
    }


def ingest_worker_status() -> dict:
    return celery_worker_status()


def ensure_worker_available() -> dict:
    if not env_bool("CELERY_REQUIRE_WORKER_ONLINE", True):
        return {"status": "not_checked"}
    worker_status = celery_worker_status()
    if worker_status.get("workers_online", 0) > 0:
        return worker_status
    raise AppError(
        "No ingestion worker is online.",
        code=ErrorCode.CONFIG_ERROR,
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        details=worker_status,
    )


def ensure_queue_configured() -> None:
    if celery_broker_url():
        return
    raise AppError(
        "Celery broker is not configured.",
        code=ErrorCode.CONFIG_ERROR,
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        details={"missing": ["CELERY_BROKER_URL"]},
    )


def enqueue_ingest_job(job_id: str, *, on_enqueued=None) -> str:
    ensure_queue_configured()
    from tasks import run_ingest_job_task

    task_id = str(uuid4())
    if on_enqueued:
        on_enqueued(task_id)
    try:
        result = run_ingest_job_task.apply_async(args=[str(job_id)], task_id=task_id)
    except (CeleryError, OperationalError, OSError) as exc:
        raise AppError(
            "Could not enqueue ingestion job.",
            code=ErrorCode.STORAGE_ERROR,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            details={"queue": "celery"},
        ) from exc
    return result.id
