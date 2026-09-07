from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status

from auth import CurrentUser, audit_event, require_roles
from errors import AppError
from rag_lab_repository import RagLabRepository
from rag_lab_service import create_publish_job, create_revision_job, query_revision, upload_files
from repositories import IngestJobRepository
from routers.documents import prepare_uploads
from schemas import (
    CreateRagLabExperimentRequest,
    CreateRagLabRevisionRequest,
    RagLabQueryRequest,
    UpdateRagLabExperimentRequest,
)
from task_queue import enqueue_rag_lab_job, ensure_queue_configured, ensure_worker_available

router = APIRouter(prefix="/admin/rag-lab", tags=["rag-lab"])


@router.post("/experiments", status_code=status.HTTP_201_CREATED)
def create_experiment(
    request: Request,
    body: CreateRagLabExperimentRequest,
    user: CurrentUser = Depends(require_roles("admin")),
):
    experiment = RagLabRepository().create_experiment(
        body.name, body.description, body.config.model_dump(mode="json"), user.id
    )
    audit_event(request, user, "rag_lab.experiment.create", "rag_lab_experiment", str(experiment["id"]))
    return {"experiment": experiment}


@router.get("/experiments")
def list_experiments(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
    _user: CurrentUser = Depends(require_roles("admin")),
):
    return RagLabRepository().list_experiments(limit=limit, offset=offset)


@router.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str, _user: CurrentUser = Depends(require_roles("admin"))):
    repository = RagLabRepository()
    return {
        "experiment": repository.get_experiment(experiment_id),
        "files": repository.list_files(experiment_id),
        "revisions": repository.list_revisions(experiment_id),
    }


@router.patch("/experiments/{experiment_id}")
def update_experiment(
    request: Request,
    experiment_id: str,
    body: UpdateRagLabExperimentRequest,
    user: CurrentUser = Depends(require_roles("admin")),
):
    updates = body.model_dump(exclude_unset=True, mode="json")
    if not updates:
        raise AppError("At least one field must be supplied.")
    experiment = RagLabRepository().update_experiment(experiment_id, updates)
    audit_event(request, user, "rag_lab.experiment.update", "rag_lab_experiment", experiment_id)
    return {"experiment": experiment}


@router.post("/experiments/{experiment_id}/files", status_code=status.HTTP_201_CREATED)
def add_experiment_files(
    request: Request,
    experiment_id: str,
    files: list[UploadFile] = File(...),
    user: CurrentUser = Depends(require_roles("admin")),
):
    uploaded = upload_files(experiment_id, prepare_uploads(files))
    audit_event(
        request,
        user,
        "rag_lab.files.upload",
        "rag_lab_experiment",
        experiment_id,
        {"files": [item["filename"] for item in uploaded]},
    )
    return {"status": "ok", "files": uploaded}


@router.post("/experiments/{experiment_id}/revisions", status_code=status.HTTP_202_ACCEPTED)
def create_revision(
    request: Request,
    experiment_id: str,
    body: CreateRagLabRevisionRequest | None = None,
    user: CurrentUser = Depends(require_roles("admin")),
):
    ensure_queue_configured()
    ensure_worker_available()
    config = body.config.model_dump(mode="json") if body and body.config else None
    revision, job = create_revision_job(experiment_id, config, user.id)
    enqueue_job_or_fail(job)
    audit_event(
        request,
        user,
        "rag_lab.revision.build",
        "rag_lab_revision",
        str(revision["id"]),
        {"job_id": str(job["id"])},
    )
    return {"revision": revision, "job": job}


@router.get("/revisions/{revision_id}/chunks")
def revision_chunks(
    revision_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    include_content: bool = Query(default=True),
    _user: CurrentUser = Depends(require_roles("admin")),
):
    repository = RagLabRepository()
    repository.get_revision(revision_id)
    return repository.list_chunks(revision_id, offset, limit, include_content)


@router.post("/revisions/{revision_id}/query")
def query_experiment_revision(
    revision_id: str,
    body: RagLabQueryRequest,
    user: CurrentUser = Depends(require_roles("admin")),
):
    retrieval_config = body.retrieval.model_dump(mode="json") if body.retrieval else None
    return query_revision(revision_id, body.question, user.id, retrieval_config)


@router.get("/revisions/{revision_id}/queries")
def list_revision_queries(
    revision_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    _user: CurrentUser = Depends(require_roles("admin")),
):
    repository = RagLabRepository()
    repository.get_revision(revision_id)
    return {"items": repository.list_trials(revision_id, limit)}


@router.post("/revisions/{revision_id}/publish", status_code=status.HTTP_202_ACCEPTED)
def publish_experiment_revision(
    request: Request,
    revision_id: str,
    user: CurrentUser = Depends(require_roles("admin")),
):
    ensure_queue_configured()
    ensure_worker_available()
    job = create_publish_job(revision_id, user.id)
    enqueue_job_or_fail(job)
    audit_event(
        request,
        user,
        "rag_lab.revision.publish",
        "rag_lab_revision",
        revision_id,
        {"job_id": str(job["id"])},
    )
    return {"job": job}


def enqueue_job_or_fail(job: dict) -> None:
    jobs = IngestJobRepository()
    try:
        enqueue_rag_lab_job(
            str(job["id"]),
            on_enqueued=lambda task_id: jobs.update(
                str(job["id"]), status="queued", metadata={"queue": "celery", "celery_task_id": task_id}
            ),
        )
    except AppError as exc:
        jobs.update(str(job["id"]), status="failed", error=exc.message, metadata={"queue": "celery"})
        raise
