import logging
from contextlib import contextmanager

from celery import Celery, signals
from celery.exceptions import Retry

from consistency import OperationBusy
from errors import AppError
from ingest_service import run_ingest_job
from rag_lab_service import run_rag_lab_job
from request_limits import acquire_worker
from security_settings import validate_security_settings
from structured_logging import configure_logging
from task_queue import (
    celery_broker_url,
    celery_result_backend,
    celery_task_max_retries,
    celery_task_retry_base_seconds,
    celery_visibility_timeout_seconds,
)

logger = logging.getLogger(__name__)

validate_security_settings()
broker_url = celery_broker_url()
if not broker_url:
    raise RuntimeError("CELERY_BROKER_URL must be set before starting the Celery worker.")

visibility_timeout = celery_visibility_timeout_seconds()

celery_app = Celery("forest_rag", broker=broker_url, backend=celery_result_backend())
celery_app.conf.update(
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": visibility_timeout},
    result_backend_transport_options={"visibility_timeout": visibility_timeout},
    task_acks_late=True,
    task_ignore_result=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
)


@signals.setup_logging.connect
def setup_worker_logging(**_kwargs) -> None:
    configure_logging()


@signals.worker_ready.connect
def log_worker_ready(sender=None, **_kwargs) -> None:
    logger.info(
        "celery_worker_ready",
        extra={"worker": str(sender), "queue": "celery", "pool": "threads", "concurrency": 1},
    )


@contextmanager
def worker_slot(task, job_id):
    try:
        lease = acquire_worker(job_id)
    except AppError as exc:
        # Admission contention/outages must not consume the job's execution retries.
        raise task.retry(countdown=int(exc.headers.get('Retry-After', '5')), max_retries=None) from exc
    try:
        yield
    finally:
        if lease:
            lease.close()


@celery_app.task(
    bind=True,
    name="documents.run_ingest_job",
    max_retries=None,
)
def run_ingest_job_task(self, job_id: str, execution_attempt: int = 0) -> None:
    logger.info(
        "ingest_task_started",
        extra={"job_id": job_id, "celery_task_id": self.request.id, "retry": execution_attempt},
    )
    try:
        with worker_slot(self, job_id):
            run_ingest_job(job_id, raise_on_failure=True, retryable=execution_attempt < celery_task_max_retries(),
                           retry_delay_seconds=celery_task_retry_base_seconds() * (2 ** execution_attempt))
    except Retry:
        raise
    except OperationBusy as exc:
        raise self.retry(countdown=int(exc.headers['Retry-After'])) from exc
    except Exception as exc:
        logger.exception(
            "ingest_task_failed",
            extra={"job_id": job_id, "celery_task_id": self.request.id, "retry": execution_attempt},
        )
        if execution_attempt >= celery_task_max_retries():
            raise

        countdown = celery_task_retry_base_seconds() * (2 ** execution_attempt)
        raise self.retry(exc=exc, countdown=countdown, kwargs={"execution_attempt": execution_attempt + 1}) from exc
    logger.info(
        "ingest_task_succeeded",
        extra={"job_id": job_id, "celery_task_id": self.request.id, "retry": execution_attempt},
    )


@celery_app.task(
    bind=True,
    name="rag_lab.run_job",
    max_retries=None,
)
def run_rag_lab_job_task(self, job_id: str, execution_attempt: int = 0) -> None:
    logger.info("rag_lab_task_started", extra={"job_id": job_id, "celery_task_id": self.request.id})
    try:
        with worker_slot(self, job_id):
            run_rag_lab_job(job_id, raise_on_failure=True, retryable=execution_attempt < celery_task_max_retries(),
                           retry_delay_seconds=celery_task_retry_base_seconds() * (2 ** execution_attempt))
    except Retry:
        raise
    except OperationBusy as exc:
        raise self.retry(countdown=int(exc.headers['Retry-After'])) from exc
    except Exception as exc:
        logger.exception("rag_lab_task_failed", extra={"job_id": job_id, "celery_task_id": self.request.id})
        if execution_attempt >= celery_task_max_retries():
            raise
        countdown = celery_task_retry_base_seconds() * (2 ** execution_attempt)
        raise self.retry(exc=exc, countdown=countdown, kwargs={"execution_attempt": execution_attempt + 1}) from exc
    logger.info("rag_lab_task_succeeded", extra={"job_id": job_id, "celery_task_id": self.request.id})
