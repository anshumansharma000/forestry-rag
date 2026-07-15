import logging

from celery import Celery, signals

from ingest_service import run_ingest_job
from repositories import IngestJobRepository
from structured_logging import configure_logging
from task_queue import (
    celery_broker_url,
    celery_result_backend,
    celery_task_max_retries,
    celery_task_retry_base_seconds,
    celery_visibility_timeout_seconds,
)

logger = logging.getLogger(__name__)

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


@celery_app.task(
    bind=True,
    name="documents.run_ingest_job",
    max_retries=celery_task_max_retries(),
)
def run_ingest_job_task(self, job_id: str) -> None:
    logger.info(
        "ingest_task_started",
        extra={"job_id": job_id, "celery_task_id": self.request.id, "retry": self.request.retries},
    )
    try:
        run_ingest_job(job_id, raise_on_failure=True)
    except Exception as exc:
        logger.exception(
            "ingest_task_failed",
            extra={"job_id": job_id, "celery_task_id": self.request.id, "retry": self.request.retries},
        )
        if self.request.retries >= self.max_retries:
            raise

        retry_count = self.request.retries + 1
        IngestJobRepository().update(
            job_id,
            status="queued",
            error=str(exc),
            metadata={
                "queue": "celery",
                "celery_task_id": self.request.id,
                "retry_count": retry_count,
                "max_retries": self.max_retries,
            },
        )
        countdown = celery_task_retry_base_seconds() * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown) from exc
    logger.info(
        "ingest_task_succeeded",
        extra={"job_id": job_id, "celery_task_id": self.request.id, "retry": self.request.retries},
    )
