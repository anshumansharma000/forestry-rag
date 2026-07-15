import os
from dataclasses import dataclass
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status

from auth import CurrentUser, audit_event, require_roles
from errors import AppError, ErrorCode
from ingest_service import create_ingest_job, get_ingest_job, mark_ingest_job_enqueue_failed, mark_ingest_job_enqueued, preview_chunks
from schemas import (
    CompleteDirectUploadFileRequest,
    CompleteDirectUploadsRequest,
    CreatePresignedUploadsRequest,
    DirectUploadFileRequest,
    IngestJobEnvelope,
    IngestRequest,
    PresignedUploadsResponse,
    UploadDocumentResponse,
    UploadDocumentsResponse,
)
from services.document_storage import document_storage
from task_queue import (
    enqueue_ingest_job,
    ensure_queue_configured,
    ensure_worker_available,
)
from task_queue import (
    ingest_worker_status as get_ingest_worker_status,
)
from upload_utils import allowed_upload_extensions, read_upload_limited, safe_filename, upload_batch_max_bytes, upload_max_bytes

router = APIRouter(tags=["documents"])


@dataclass(frozen=True)
class PendingUpload:
    filename: str
    content: bytes
    content_type: str | None


@dataclass(frozen=True)
class PendingDirectUpload:
    filename: str
    size_bytes: int
    content_type: str


@dataclass(frozen=True)
class CompletedDirectUpload:
    upload_id: UUID
    filename: str


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED, response_model=IngestJobEnvelope)
def ingest(
    request: Request,
    request_body: IngestRequest | None = None,
    user: CurrentUser = Depends(require_roles("knowledge_manager")),
):
    ensure_queue_configured()
    ensure_worker_available()
    execution_mode = "celery"
    source = None
    if request_body and request_body.source:
        source = validate_upload_filename(request_body.source, allowed_upload_extensions())
    job = create_ingest_job(user.id, source=source)
    try:
        enqueue_ingest_job(
            job["id"],
            on_enqueued=lambda queued_task_id: mark_ingest_job_enqueued(
                job["id"],
                task_id=queued_task_id,
                queue=execution_mode,
            ),
        )
    except AppError as exc:
        mark_ingest_job_enqueue_failed(job["id"], error=exc.message, queue=execution_mode)
        raise
    audit_event(
        request,
        user,
        "documents.ingest.requested",
        "ingest_job",
        job["id"],
        {"source": source, "scope": "document" if source else "corpus"},
    )
    return {"job": job}


@router.get("/ingest/worker/status")
def ingest_worker_status(_user: CurrentUser = Depends(require_roles("knowledge_manager"))):
    return get_ingest_worker_status()


@router.get("/ingest/jobs/{job_id}", response_model=IngestJobEnvelope)
def ingest_job(job_id: str, _user: CurrentUser = Depends(require_roles("knowledge_manager"))):
    job = get_ingest_job(job_id)
    if not job:
        raise AppError("Ingestion job not found.", code=ErrorCode.NOT_FOUND, status_code=status.HTTP_404_NOT_FOUND)
    return {"job": job}


@router.post("/documents/upload", response_model=UploadDocumentResponse)
def upload_document(
    request: Request,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_roles("knowledge_manager")),
):
    [pending_upload] = prepare_uploads([file])
    return save_uploads(request, user, [pending_upload])[0]


@router.post("/documents/uploads", response_model=UploadDocumentsResponse)
def upload_documents(
    request: Request,
    files: list[UploadFile] = File(...),
    user: CurrentUser = Depends(require_roles("knowledge_manager")),
):
    pending_uploads = prepare_uploads(files)
    return {"status": "ok", "files": save_uploads(request, user, pending_uploads)}


@router.post("/documents/uploads/presign", response_model=PresignedUploadsResponse)
def create_presigned_document_uploads(
    request_body: CreatePresignedUploadsRequest,
    user: CurrentUser = Depends(require_roles("knowledge_manager")),
):
    pending_uploads = prepare_direct_uploads(request_body.files)
    return {"status": "ok", "uploads": create_presigned_uploads(user, pending_uploads)}


@router.post("/documents/uploads/complete", response_model=UploadDocumentsResponse)
def complete_presigned_document_uploads(
    request: Request,
    request_body: CompleteDirectUploadsRequest,
    user: CurrentUser = Depends(require_roles("knowledge_manager")),
):
    completed_uploads = prepare_completed_direct_uploads(request_body.files)
    return {"status": "ok", "files": complete_direct_uploads(request, user, completed_uploads)}


def prepare_uploads(files: list[UploadFile]) -> list[PendingUpload]:
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one file is required")

    allowed_extensions = allowed_upload_extensions()
    max_bytes = upload_max_bytes()
    pending_uploads = []
    seen_filenames = set()
    batch_bytes = 0
    for file in files:
        filename = safe_filename(file.filename or "")
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix not in allowed_extensions:
            allowed = ", ".join(f".{ext}" for ext in sorted(allowed_extensions))
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Only {allowed} files are supported")
        if filename in seen_filenames:
            raise AppError(
                "Duplicate filenames are not allowed in the same upload request.",
                code=ErrorCode.CONFLICT,
                status_code=status.HTTP_409_CONFLICT,
                details={"filename": filename},
            )
        seen_filenames.add(filename)

        content = read_upload_limited(file, max_bytes)
        if not content:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")
        batch_bytes += len(content)
        if batch_bytes > upload_batch_max_bytes():
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Upload batch exceeds limit of {upload_batch_max_bytes()} bytes",
            )
        pending_uploads.append(PendingUpload(filename=filename, content=content, content_type=file.content_type))
    return pending_uploads


def prepare_direct_uploads(files: list[DirectUploadFileRequest]) -> list[PendingDirectUpload]:
    allowed_extensions = allowed_upload_extensions()
    max_bytes = upload_max_bytes()
    pending_uploads = []
    seen_filenames = set()
    for file in files:
        filename = validate_upload_filename(file.filename, allowed_extensions)
        if filename in seen_filenames:
            raise_duplicate_filename(filename)
        seen_filenames.add(filename)
        if file.size_bytes > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds upload limit of {max_bytes} bytes",
            )
        pending_uploads.append(
            PendingDirectUpload(
                filename=filename,
                size_bytes=file.size_bytes,
                content_type=(file.content_type or "application/octet-stream").strip() or "application/octet-stream",
            )
        )
    return pending_uploads


def prepare_completed_direct_uploads(files: list[CompleteDirectUploadFileRequest]) -> list[CompletedDirectUpload]:
    allowed_extensions = allowed_upload_extensions()
    completed_uploads = []
    seen_filenames = set()
    seen_upload_ids = set()
    for file in files:
        filename = validate_upload_filename(file.filename, allowed_extensions)
        if filename in seen_filenames:
            raise_duplicate_filename(filename)
        if file.upload_id in seen_upload_ids:
            raise AppError(
                "Duplicate upload IDs are not allowed in the same completion request.",
                code=ErrorCode.CONFLICT,
                status_code=status.HTTP_409_CONFLICT,
                details={"upload_id": str(file.upload_id)},
            )
        seen_filenames.add(filename)
        seen_upload_ids.add(file.upload_id)
        completed_uploads.append(CompletedDirectUpload(upload_id=file.upload_id, filename=filename))
    return completed_uploads


def create_presigned_uploads(user: CurrentUser, pending_uploads: list[PendingDirectUpload]) -> list[dict]:
    storage = direct_upload_storage()
    ensure_direct_upload_prefix_is_isolated(storage)
    if not document_replace_allowed():
        ensure_no_storage_conflicts(storage, [upload.filename for upload in pending_uploads])

    expires_in_seconds = presigned_upload_expires_seconds()
    uploads = []
    for upload in pending_uploads:
        upload_id = uuid4()
        key = staging_key_for(user.id, upload_id, upload.filename)
        uploads.append(
            {
                "upload_id": upload_id,
                "filename": upload.filename,
                "upload_url": storage.presigned_put_url(key, upload.content_type, expires_in_seconds),
                "method": "PUT",
                "headers": {"Content-Type": upload.content_type},
                "expires_in_seconds": expires_in_seconds,
                "max_bytes": upload_max_bytes(),
            }
        )
    return uploads


def complete_direct_uploads(request: Request, user: CurrentUser, completed_uploads: list[CompletedDirectUpload]) -> list[dict[str, str]]:
    storage = direct_upload_storage()
    ensure_direct_upload_prefix_is_isolated(storage)
    allow_replace = document_replace_allowed()
    max_bytes = upload_max_bytes()
    staged_objects = []

    for upload in completed_uploads:
        staging_key = staging_key_for(user.id, upload.upload_id, upload.filename)
        head = storage.head_key(staging_key)
        if not head:
            raise AppError(
                "Uploaded document was not found. The direct upload may still be in progress or the URL may have expired.",
                code=ErrorCode.NOT_FOUND,
                status_code=status.HTTP_404_NOT_FOUND,
                details={"filename": upload.filename, "upload_id": str(upload.upload_id)},
            )
        content_length = int(head.get("ContentLength") or 0)
        if content_length <= 0:
            storage.delete_key(staging_key)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")
        if content_length > max_bytes:
            storage.delete_key(staging_key)
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds upload limit of {max_bytes} bytes",
            )
        staged_objects.append((upload, staging_key, content_length, head.get("ContentType")))

    if not allow_replace:
        try:
            ensure_no_storage_conflicts(storage, [upload.filename for upload in completed_uploads])
        except AppError:
            for _upload, staging_key, _content_length, _content_type in staged_objects:
                storage.delete_key(staging_key)
            raise

    results = []
    for upload, staging_key, content_length, content_type in staged_objects:
        destination_key = storage.key_for(upload.filename)
        path = storage.copy_key(staging_key, destination_key)
        storage.delete_key(staging_key)
        audit_event(
            request,
            user,
            "documents.upload",
            "document",
            upload.filename,
            {
                "filename": upload.filename,
                "bytes": content_length,
                "content_type": content_type,
                "direct_upload": True,
                "upload_id": str(upload.upload_id),
            },
        )
        results.append({"status": "ok", "filename": upload.filename, "path": path})
    return results


def save_uploads(request: Request, user: CurrentUser, pending_uploads: list[PendingUpload]) -> list[dict[str, str]]:
    storage = document_storage()
    if not document_replace_allowed():
        ensure_no_storage_conflicts(storage, [upload.filename for upload in pending_uploads])

    results = []
    for upload in pending_uploads:
        path = storage.save(upload.filename, upload.content)
        audit_event(
            request,
            user,
            "documents.upload",
            "document",
            upload.filename,
            {"filename": upload.filename, "bytes": len(upload.content), "content_type": upload.content_type},
        )
        results.append({"status": "ok", "filename": upload.filename, "path": path})
    return results


def validate_upload_filename(filename: str, allowed_extensions: set[str]) -> str:
    filename = safe_filename(filename)
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in allowed_extensions:
        allowed = ", ".join(f".{ext}" for ext in sorted(allowed_extensions))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Only {allowed} files are supported")
    return filename


def raise_duplicate_filename(filename: str) -> None:
    raise AppError(
        "Duplicate filenames are not allowed in the same upload request.",
        code=ErrorCode.CONFLICT,
        status_code=status.HTTP_409_CONFLICT,
        details={"filename": filename},
    )


def ensure_no_storage_conflicts(storage, filenames: list[str]) -> None:
    for filename in filenames:
        if storage.exists(filename):
            raise AppError(
                "A document with this filename already exists.",
                code=ErrorCode.CONFLICT,
                status_code=status.HTTP_409_CONFLICT,
                details={"filename": filename},
            )


def direct_upload_storage():
    storage = document_storage()
    if not all(hasattr(storage, method) for method in ("presigned_put_url", "head_key", "copy_key", "delete_key", "key_for")):
        raise AppError(
            "Direct uploads require DOCUMENT_STORAGE_BACKEND=r2.",
            code=ErrorCode.CONFIG_ERROR,
            status_code=status.HTTP_400_BAD_REQUEST,
            details={"backend": getattr(storage, "backend", None)},
        )
    return storage


def ensure_direct_upload_prefix_is_isolated(storage) -> None:
    document_prefix = getattr(storage, "prefix", "")
    staging_prefix = normalized_staging_prefix()
    if not document_prefix:
        raise AppError(
            "Direct uploads require a non-empty R2_PREFIX so temporary uploads stay hidden from ingestion.",
            code=ErrorCode.CONFIG_ERROR,
            details={"setting": "R2_PREFIX"},
        )
    if staging_prefix.startswith(document_prefix):
        raise AppError(
            "R2_UPLOAD_STAGING_PREFIX must not be inside R2_PREFIX.",
            code=ErrorCode.CONFIG_ERROR,
            details={"R2_PREFIX": document_prefix, "R2_UPLOAD_STAGING_PREFIX": staging_prefix},
        )


def document_replace_allowed() -> bool:
    return os.getenv("ALLOW_DOCUMENT_REPLACE", "false").strip().lower() in {"1", "true", "yes"}


def presigned_upload_expires_seconds() -> int:
    raw = os.getenv("PRESIGNED_UPLOAD_EXPIRES_SECONDS", "900")
    try:
        value = int(raw)
    except ValueError as exc:
        raise AppError("PRESIGNED_UPLOAD_EXPIRES_SECONDS must be an integer.", code=ErrorCode.CONFIG_ERROR) from exc
    if value <= 0:
        raise AppError("PRESIGNED_UPLOAD_EXPIRES_SECONDS must be greater than 0.", code=ErrorCode.CONFIG_ERROR)
    return value


def staging_key_for(user_id: str, upload_id: UUID, filename: str) -> str:
    return f"{normalized_staging_prefix()}{user_id}/{upload_id}/{filename}"


def normalized_staging_prefix() -> str:
    prefix = os.getenv("R2_UPLOAD_STAGING_PREFIX", "pending-uploads/").strip().lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix = f"{prefix}/"
    return prefix


@router.get("/chunks/preview")
def chunks_preview(
    source: str | None = Query(default=None, min_length=1, max_length=255),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    include_content: bool = Query(default=False),
    max_content_chars: int = Query(default=500, ge=0, le=5000),
    all_sources: bool = Query(default=False),
    _user: CurrentUser = Depends(require_roles("knowledge_manager")),
):
    return preview_chunks(
        source=source,
        limit=limit,
        offset=offset,
        include_content=include_content,
        max_content_chars=max_content_chars,
        all_sources=all_sources,
    )
