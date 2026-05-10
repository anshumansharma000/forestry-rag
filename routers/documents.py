from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile, status

from auth import CurrentUser, audit_event, require_roles
from errors import AppError, ErrorCode
from ingest_service import create_ingest_job, get_ingest_job, preview_chunks, run_ingest_job
from schemas import IngestJobEnvelope, UploadDocumentResponse
from services.document_storage import document_storage
from upload_utils import allowed_upload_extensions, read_upload_limited, safe_filename, upload_max_bytes

router = APIRouter(tags=["documents"])


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED, response_model=IngestJobEnvelope)
def ingest(
    background_tasks: BackgroundTasks,
    request: Request,
    user: CurrentUser = Depends(require_roles("knowledge_manager")),
):
    job = create_ingest_job(user.id)
    background_tasks.add_task(run_ingest_job, job["id"])
    audit_event(request, user, "documents.ingest.requested", "ingest_job", job["id"])
    return {"job": job}


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
    filename = safe_filename(file.filename or "")
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed_extensions = allowed_upload_extensions()
    if suffix not in allowed_extensions:
        allowed = ", ".join(f".{ext}" for ext in sorted(allowed_extensions))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Only {allowed} files are supported")

    content = read_upload_limited(file, upload_max_bytes())
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    storage = document_storage()
    if storage.exists(filename):
        import os

        if os.getenv("ALLOW_DOCUMENT_REPLACE", "false").strip().lower() not in {"1", "true", "yes"}:
            raise AppError(
                "A document with this filename already exists.",
                code=ErrorCode.CONFLICT,
                status_code=status.HTTP_409_CONFLICT,
            )

    path = storage.save(filename, content)
    audit_event(
        request,
        user,
        "documents.upload",
        "document",
        filename,
        {"filename": filename, "bytes": len(content), "content_type": file.content_type},
    )
    return {"status": "ok", "filename": filename, "path": path}


@router.get("/chunks/preview")
def chunks_preview(_user: CurrentUser = Depends(require_roles("knowledge_manager"))):
    return preview_chunks()
