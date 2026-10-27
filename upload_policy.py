"""A confirmed recovery upload cannot replace a searchable document."""

from contextlib import contextmanager

from errors import AppError, ErrorCode
from repositories import DocumentRepository


def ensure_upload_conflicts(storage, uploads, repository=None):
    conflicts = []
    for index, upload in enumerate(uploads):
        exists = storage.exists(upload.filename)
        repository = repository or DocumentRepository()
        state = repository.upload_state(upload.filename)
        if not exists and not upload.replace_failed and state not in {"indexed", "processing"}:
            continue
        recoverable = state in {"failed", "unindexed"}
        if upload.replace_failed and recoverable:
            continue
        conflicts.append({
            "index": index, "filename": upload.filename, "state": state,
            "can_replace_failed": recoverable, "requires_confirmation": recoverable,
        })
    if not conflicts:
        return
    blocked = [item for item in conflicts if not item["can_replace_failed"]]
    if blocked:
        names = ", ".join(item["filename"] for item in blocked)
        message = f"These files are already indexed or processing and cannot be replaced: {names}."
    else:
        message = "These filenames already exist but have no successful index. Confirm replacement to upload and process them again."
    raise AppError(message, code=ErrorCode.CONFLICT, status_code=409, details={
        "filename": conflicts[0]["filename"], "conflicts": conflicts,
        "can_replace_failed": not blocked, "requires_confirmation": not blocked,
    })


@contextmanager
def upload_write_guard(storage, uploads):
    repository = DocumentRepository()
    # Every final storage write shares the ingestion/deletion lease, including
    # new uploads whose original may have disappeared during a deletion retry.
    with repository.index_lease() as lease:
        ensure_upload_conflicts(storage, uploads, repository)
        yield lease
