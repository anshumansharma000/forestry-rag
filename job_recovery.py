"""Durable dispatch repair; safe to run in every API replica."""
import asyncio
import logging

from starlette.concurrency import run_in_threadpool

from repositories import IngestJobRepository
from task_queue import celery_broker_url, enqueue_ingest_job, enqueue_rag_lab_job

logger = logging.getLogger(__name__)


def recover_jobs():
    if not celery_broker_url():
        return
    repository = IngestJobRepository()
    for job in repository.recover():
        try:
            enqueue = enqueue_ingest_job if job['kind'] == 'documents.ingest' else enqueue_rag_lab_job
            task_id = enqueue(str(job['id']))
            repository.update(str(job['id']), status='queued', metadata={'celery_task_id': task_id, 'queue': 'celery'})
        except Exception:
            # Row remains queued; another sweep repairs a failed/ambiguous dispatch.
            logger.exception('job_recovery_dispatch_failed', extra={'job_id': job['id']})


async def recovery_loop():
    while True:
        try:
            await run_in_threadpool(recover_jobs)
        except Exception:
            logger.exception('job_recovery_failed')
        await asyncio.sleep(60)
