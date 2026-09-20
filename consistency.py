"""Database leases and commit fencing for retried application operations."""
import logging
import math
import threading
from datetime import UTC, datetime
from uuid import uuid4

from errors import AppError, ErrorCode

logger = logging.getLogger(__name__)


class OperationBusy(AppError):
    def __init__(self, retry_after: int = 5):
        super().__init__('This operation is already running. Retry shortly.', code=ErrorCode.CONFLICT,
                         status_code=409, headers={'Retry-After': str(retry_after)})


class DatabaseLease:
    def __init__(self, client, name: str, *, token: str | None = None, claimed: bool = False):
        self.client, self.name = client, name
        self.token = token or str(uuid4())
        self.claimed = claimed
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = None

    def rpc(self, function):
        return self.client.rpc(function, {'p_name': self.name, 'p_token': self.token}).execute().data

    def __enter__(self):
        if not self.claimed and not self.rpc('claim_operation'):
            raise OperationBusy()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True, name='database-operation-lease')
        self.thread.start()
        return self

    def _heartbeat(self):
        while not self.stop.wait(60):
            try:
                if not self.rpc('renew_operation'):
                    self.lost.set()
                    return
            except Exception:
                self.lost.set()
                logger.exception('database_lease_renewal_failed')
                return

    def check(self):
        if self.lost.is_set():
            raise OperationBusy()
        self.rpc('assert_operation')

    def __exit__(self, *_args):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        try:
            self.rpc('release_operation')
        except Exception:
            logger.exception('database_lease_release_failed')


def check_job_schedule(job):
    if job.get('available_at'):
        available = datetime.fromisoformat(job['available_at'].replace('Z', '+00:00'))
        delay = math.ceil((available - datetime.now(UTC)).total_seconds())
        if delay > 0:
            raise OperationBusy(delay)
