import json
import mimetypes
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter, ValidationError

from auth import CurrentUser, audit_event, require_exact_admin, require_roles
from document_categorization import CATEGORY_LABELS, CategorizationInput, DocumentCategory
from errors import AppError, ErrorCode
from ingest_service import create_ingest_job, get_ingest_job, mark_ingest_job_enqueue_failed, mark_ingest_job_enqueued, preview_chunks
from repositories import DocumentRepository
from schemas import (
    CompleteDirectUploadFileRequest,
    CompleteDirectUploadsRequest,
    CreatePresignedUploadsRequest,
    DirectUploadFileRequest,
    DocumentLibraryResponse,
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
from upload_policy import ensure_upload_conflicts, upload_write_guard
from upload_utils import (
    allowed_upload_extensions,
    read_upload_limited,
    safe_filename,
    upload_batch_max_bytes,
    upload_batch_max_files,
    upload_max_bytes,
)

router = APIRouter(tags=["documents"])

DOWNLOAD_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain; charset=utf-8",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


@router.get("/documents", response_model=DocumentLibraryResponse, response_model_exclude_unset=True)
def list_documents(
    status_filter: Literal["indexed", "failed"] = Query(default="indexed", alias="status"),
    search: str | None = Query(default=None, min_length=1, max_length=200),
    kind: Literal["pdf", "docx", "txt", "ppt", "pptx"] | None = None,
    document_type: str | None = Query(default=None, min_length=1, max_length=50),
    year: str | None = Query(default=None, pattern=r"^(?:19|20)\d{2}$"),
    sort_by: Literal["updated_at", "created_at", "title", "source", "page_count"] = "updated_at",
    sort_order: Literal["asc", "desc"] = "desc",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
    _user: CurrentUser = Depends(require_roles("knowledge_manager")),
    category: DocumentCategory | None = None,
):
    return DocumentRepository().list_documents(
        ingest_status=status_filter,
        search=search.strip() if search else None,
        kind=kind,
        document_type=document_type.strip().lower() if document_type else None,
        year=year,
        sort_by=sort_by,
        sort_order=sort_order,
        offset=offset,
        limit=limit,
        **({"category": category} if category else {}),
    )


@router.get("/documents/categories")
def document_categories(_user: CurrentUser = Depends(require_roles("knowledge_manager"))):
    return {"categories": [{"value": value, "label": label} for value, label in CATEGORY_LABELS.items()]}


@router.get("/documents/{document_id}/categorization")
def get_document_categorization(
    document_id: UUID,
    _user: CurrentUser = Depends(require_roles("knowledge_manager")),
):
    document = DocumentRepository().get_document(str(document_id))
    if not document:
        raise AppError("Document not found.", code=ErrorCode.NOT_FOUND, status_code=404)
    return {"document_id": str(document_id), "categorization": (document.get("metadata") or {}).get("categorization")}


@router.patch("/documents/{document_id}/categorization")
def update_document_categorization(
    document_id: UUID,
    body: CategorizationInput,
    request: Request,
    user: CurrentUser = Depends(require_roles("knowledge_manager")),
):
    repository = DocumentRepository()
    document = repository.get_document(str(document_id))
    if not document:
        raise AppError("Document not found.", code=ErrorCode.NOT_FOUND, status_code=404)
    repository.check_categorization_support()
    categorization = repository.save_categorization(document["source"], body.model_dump(mode="json", exclude_unset=True), user.id)
    audit_event(request, user, "documents.categorization.updated", "document", str(document_id),
                {"fields": sorted(body.model_fields_set)})
    return {"document_id": str(document_id), "categorization": categorization}


@router.get(
    "/documents/{document_id}/download",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "The original document file.",
            "content": {"application/octet-stream": {}},
        },
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "The authenticated user is not an admin."},
        404: {"description": "The document or original file is unavailable."},
    },
)
def download_document(
    document_id: str,
    request: Request,
    user: CurrentUser = Depends(require_exact_admin),
):
    # The current role comes from the server-side user row in get_current_user;
    # no role claim or frontend state is trusted for this authorization decision.
    document = DocumentRepository().get_document(document_id)
    if not document:
        raise document_download_not_found()

    original_filename = document.get("source") or ""
    suffix = Path(original_filename).suffix.lower()
    content_type = DOWNLOAD_CONTENT_TYPES.get(suffix) or mimetypes.guess_type(original_filename)[0]
    if not content_type:
        content_type = "application/octet-stream"
    download = document_storage().open_download(original_filename, content_type)
    if download is None:
        raise document_download_not_found()

    safe_name = safe_download_filename(download.filename)
    headers = {"Content-Disposition": content_disposition(safe_name)}
    if download.content_length is not None:
        headers["Content-Length"] = str(download.content_length)

    def audited_chunks():
        for chunk in download.chunks:
            if chunk:
                yield chunk
        audit_event(
            request,
            user,
            "documents.download",
            "document",
            str(document["id"]),
            {
                "document_id": str(document["id"]),
                "downloaded_at": datetime.now(UTC).isoformat(),
            },
        )

    return StreamingResponse(audited_chunks(), media_type=download.content_type, headers=headers)


def document_download_not_found() -> AppError:
    return AppError(
        "Document or original file not found.",
        code=ErrorCode.NOT_FOUND,
        status_code=status.HTTP_404_NOT_FOUND,
    )


def safe_download_filename(filename: str) -> str:
    """Produce a header-safe basename while retaining an allowed extension."""
    basename = Path(filename.replace("\\", "/")).name
    basename = re.sub(r"[\x00-\x1f\x7f]", "", basename).strip()
    suffix = Path(basename).suffix.lower()
    preserved_suffix = suffix if suffix in DOWNLOAD_CONTENT_TYPES else ""
    stem = basename[: -len(suffix)] if suffix else basename
    stem = stem.replace('"', "").replace(";", "").strip(" .")
    if not stem:
        stem = "document"
    return f"{stem}{preserved_suffix}"


def content_disposition(filename: str) -> str:
    extension = Path(filename).suffix
    stem = filename[: -len(extension)] if extension else filename
    normalized = unicodedata.normalize("NFKD", stem)
    ascii_stem = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_stem = re.sub(r"[^A-Za-z0-9._ -]", "_", ascii_stem).strip(" .") or "document"
    ascii_name = f"{ascii_stem}{extension}"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


@dataclass(frozen=True)
class PendingUpload:
    filename: str
    content: bytes
    content_type: str | None
    categorization: CategorizationInput | None = None
    replace_failed: bool = False


@dataclass(frozen=True)
class PendingDirectUpload:
    filename: str
    size_bytes: int
    content_type: str
    replace_failed: bool = False


@dataclass(frozen=True)
class CompletedDirectUpload:
    upload_id: UUID
    filename: str
    categorization: CategorizationInput | None = None
    replace_failed: bool = False


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


@router.post("/documents/upload", response_model=UploadDocumentResponse, response_model_exclude_unset=True)
def upload_document(
    request: Request,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_roles("knowledge_manager")),
    categorization: str | None = Form(default=None, max_length=50000),
    replace_failed: bool = Form(default=False),
):
    metadata = parse_upload_categorizations(categorization, single=True)
    [pending_upload] = prepare_uploads([file], metadata, replace_failed=replace_failed)
    return save_uploads(request, user, [pending_upload])[0]


@router.post("/documents/uploads", response_model=UploadDocumentsResponse, response_model_exclude_unset=True)
def upload_documents(
    request: Request,
    files: list[UploadFile] = File(...),
    user: CurrentUser = Depends(require_roles("knowledge_manager")),
    categorizations: str | None = Form(default=None, max_length=250000),
    replace_failed: bool = Form(default=False),
):
    pending_uploads = prepare_uploads(files, parse_upload_categorizations(categorizations), replace_failed=replace_failed)
    return {"status": "ok", "files": save_uploads(request, user, pending_uploads)}


@router.post("/documents/uploads/presign", response_model=PresignedUploadsResponse, response_model_exclude_unset=True)
def create_presigned_document_uploads(
    request_body: CreatePresignedUploadsRequest,
    user: CurrentUser = Depends(require_roles("knowledge_manager")),
):
    pending_uploads = prepare_direct_uploads(request_body.files)
    return {"status": "ok", "uploads": create_presigned_uploads(user, pending_uploads)}


@router.post("/documents/uploads/complete", response_model=UploadDocumentsResponse, response_model_exclude_unset=True)
def complete_presigned_document_uploads(
    request: Request,
    request_body: CompleteDirectUploadsRequest,
    user: CurrentUser = Depends(require_roles("knowledge_manager")),
):
    completed_uploads = prepare_completed_direct_uploads(request_body.files)
    return {"status": "ok", "files": complete_direct_uploads(request, user, completed_uploads)}


def parse_upload_categorizations(raw: str | None, *, single: bool = False):
    if raw is None:
        return None
    try:
        if single:
            return CategorizationInput.model_validate_json(raw)
        return TypeAdapter(dict[str, CategorizationInput]).validate_python(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="Invalid categorization. Use documented category values and metadata fields.") from exc


def prepare_uploads(
    files: list[UploadFile], categorizations: dict[str, CategorizationInput] | CategorizationInput | None = None,
    *, replace_failed: bool = False,
) -> list[PendingUpload]:
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one file is required")
    if len(files) > upload_batch_max_files():
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload batch exceeds limit of {upload_batch_max_files()} files",
        )

    allowed_extensions = allowed_upload_extensions()
    if isinstance(categorizations, dict):
        filenames = {file.filename for file in files}
        if set(categorizations) - filenames:
            raise HTTPException(status_code=422, detail="Categorization keys must match uploaded filenames exactly.")
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
        metadata = categorizations.get(file.filename) if isinstance(categorizations, dict) else categorizations
        pending_uploads.append(PendingUpload(
            filename=filename, content=content, content_type=file.content_type, categorization=metadata, replace_failed=replace_failed,
        ))
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
                replace_failed=file.replace_failed,
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
        completed_uploads.append(CompletedDirectUpload(
            upload_id=file.upload_id, filename=filename, categorization=file.categorization, replace_failed=file.replace_failed,
        ))
    return completed_uploads


def create_presigned_uploads(user: CurrentUser, pending_uploads: list[PendingDirectUpload]) -> list[dict]:
    storage = direct_upload_storage()
    ensure_direct_upload_prefix_is_isolated(storage)
    ensure_upload_conflicts(storage, pending_uploads)

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
                **({"replace_failed": True} if upload.replace_failed else {}),
            }
        )
    return uploads


def complete_direct_uploads(request: Request, user: CurrentUser, completed_uploads: list[CompletedDirectUpload]) -> list[dict[str, str]]:
    if any(upload.categorization is not None for upload in completed_uploads):
        DocumentRepository().check_categorization_support()
    storage = direct_upload_storage()
    ensure_direct_upload_prefix_is_isolated(storage)
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

    try:
        with upload_write_guard(storage, completed_uploads) as lease:
            results = []
            for upload, staging_key, content_length, content_type in staged_objects:
                if lease is not None:
                    lease.check()
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
                        "replaced_failed": upload.replace_failed,
                        "upload_id": str(upload.upload_id),
                    },
                )
                result = {"status": "ok", "filename": upload.filename, "path": path}
                if upload.categorization is not None:
                    result["categorization"] = persist_upload_categorization(upload.filename, upload.categorization, user.id)
                results.append(result)
        return results
    except AppError:
        for _upload, staging_key, _content_length, _content_type in staged_objects:
            storage.delete_key(staging_key)
        raise


def save_uploads(request: Request, user: CurrentUser, pending_uploads: list[PendingUpload]) -> list[dict[str, str]]:
    if any(upload.categorization is not None for upload in pending_uploads):
        DocumentRepository().check_categorization_support()
    storage = document_storage()
    with upload_write_guard(storage, pending_uploads) as lease:
        results = []
        for upload in pending_uploads:
            if lease is not None:
                lease.check()
            path = storage.save(upload.filename, upload.content)
            audit_event(
                request,
                user,
                "documents.upload",
                "document",
                upload.filename,
                {"filename": upload.filename, "bytes": len(upload.content),
                 "content_type": upload.content_type, "replaced_failed": upload.replace_failed},
            )
            result = {"status": "ok", "filename": upload.filename, "path": path}
            if upload.categorization is not None:
                result["categorization"] = persist_upload_categorization(upload.filename, upload.categorization, user.id)
            results.append(result)
    return results


def persist_upload_categorization(filename: str, categorization: CategorizationInput, user_id: str) -> dict:
    return DocumentRepository().save_categorization(filename, categorization.model_dump(mode="json", exclude_unset=True), user_id)


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
