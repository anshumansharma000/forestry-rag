import hashlib
import time
from uuid import uuid4

from chunking import iter_document_chunks
from documents import read_document
from errors import AppError, ErrorCode
from prompts import answer_is_abstention, answer_with_gemini
from rag_lab_repository import RagLabRepository
from repositories import IngestJobRepository
from retrieval import embed_texts, embedding_text, retrieval_confidence, retrieve, source_payload
from services.rag_lab_storage import RagLabStorage, rag_lab_storage
from settings import env_int


def upload_files(experiment_id: str, uploads: list, repository=None, storage=None) -> list[dict]:
    repository = repository or RagLabRepository()
    storage = storage or rag_lab_storage()
    experiment = repository.get_experiment(experiment_id)
    if experiment["status"] in {"building", "publishing", "archived"}:
        raise AppError("Files cannot be added in the experiment's current state.", code=ErrorCode.CONFLICT, status_code=409)

    existing_names = {item["filename"] for item in repository.list_files(experiment_id)}
    results = []
    for upload in uploads:
        if upload.filename in existing_names:
            raise AppError(
                "A file with this name already exists in the experiment.",
                code=ErrorCode.CONFLICT,
                status_code=409,
                details={"filename": upload.filename},
            )
        file_id = str(uuid4())
        key = storage.file_key(experiment_id, file_id, upload.filename)
        storage.save_bytes(key, upload.content, upload.content_type)
        row = repository.add_file(
            {
                "id": file_id,
                "experiment_id": experiment_id,
                "filename": upload.filename,
                "kind": upload.filename.rsplit(".", 1)[-1].lower(),
                "storage_key": key,
                "checksum_sha256": hashlib.sha256(upload.content).hexdigest(),
                "size_bytes": len(upload.content),
            }
        )
        existing_names.add(upload.filename)
        results.append(row)
    repository.update_experiment(experiment_id, {"status": "draft"})
    return results


def create_revision_job(experiment_id: str, config: dict | None, actor_user_id: str, repository=None) -> tuple[dict, dict]:
    repository = repository or RagLabRepository()
    experiment = repository.get_experiment(experiment_id)
    if experiment["status"] in {"building", "publishing", "archived"}:
        raise AppError(
            "A revision cannot be created in the experiment's current state.",
            code=ErrorCode.CONFLICT,
            status_code=409,
        )
    if not repository.list_files(experiment_id):
        raise AppError("Upload at least one file before building a revision.", code=ErrorCode.INVALID_INPUT)
    snapshot = config or experiment["config"]
    revision = repository.create_revision(experiment_id, snapshot, actor_user_id)
    job = IngestJobRepository(repository.client).create(
        actor_user_id,
        kind="rag_lab.build",
        metadata={"revision_id": revision["id"], "experiment_id": experiment_id},
    )
    return revision, job


def build_revision(revision_id: str, repository=None, storage: RagLabStorage | None = None) -> dict:
    repository = repository or RagLabRepository()
    storage = storage or rag_lab_storage()
    revision = repository.get_revision(revision_id)
    files = repository.list_files(revision["experiment_id"])
    config = revision["config"]["chunking"]
    if revision["status"] == "queued":
        repository.delete_revision_chunks(revision_id)
        existing_chunk_keys: set[tuple[str, int]] = set()
    else:
        existing_chunk_keys = repository.existing_chunk_keys(revision_id)
    repository.update_revision(revision_id, status="building")
    inserted = len(existing_chunk_keys)
    embedding_batch_size = min(env_int("GEMINI_EMBEDDING_BATCH_SIZE", 2), 100)
    try:
        for file in files:
            doc = extracted_document(file, storage, repository)
            batch: list[tuple[dict, str]] = []
            for chunk in iter_document_chunks(
                doc,
                max_tokens=config["max_tokens"],
                overlap_tokens=config["overlap_tokens"],
                profile=config.get("profile", "auto"),
            ):
                chunk_key = (file["id"], chunk["chunk_index"])
                if chunk_key in existing_chunk_keys:
                    continue
                batch.append(
                    ({
                        "revision_id": revision_id,
                        "file_id": file["id"],
                        "source": file["filename"],
                        "chunk_index": chunk["chunk_index"],
                        "chunk_type": chunk["chunk_type"],
                        "section_heading": chunk["section_heading"],
                        "page_start": chunk["page_start"],
                        "page_end": chunk["page_end"],
                        "content": chunk["content"],
                        "token_estimate": chunk["token_estimate"],
                        "metadata": {**chunk["metadata"], "file_id": file["id"], "display_source": file["filename"]},
                    }, embedding_text(chunk))
                )
                if len(batch) >= embedding_batch_size:
                    inserted += insert_embedded_batch(repository, batch)
                    existing_chunk_keys.update((row["file_id"], row["chunk_index"]) for row, _ in batch)
                    batch.clear()
            if batch:
                inserted += insert_embedded_batch(repository, batch)
                existing_chunk_keys.update((row["file_id"], row["chunk_index"]) for row, _ in batch)
        repository.update_revision(revision_id, status="ready", chunk_count=inserted)
        repository.update_experiment(revision["experiment_id"], {"status": "ready"})
        return {"revision_id": revision_id, "documents": len(files), "chunks": inserted}
    except Exception as exc:
        repository.update_revision(revision_id, status="failed", chunk_count=inserted, error=str(exc))
        repository.update_experiment(revision["experiment_id"], {"status": "failed"})
        raise


def insert_embedded_batch(repository: RagLabRepository, batch: list[tuple[dict, str]]) -> int:
    embeddings = embed_texts([text for _, text in batch])
    rows = [{**row, "embedding": embedding} for (row, _), embedding in zip(batch, embeddings, strict=True)]
    return repository.insert_chunk_batch(rows)


def extracted_document(file: dict, storage: RagLabStorage, repository: RagLabRepository) -> dict:
    if file.get("extraction_key"):
        return storage.load_json(file["extraction_key"])
    with storage.document_file(file["storage_key"], file["filename"]) as document_file:
        doc = read_document(document_file)
    if not doc:
        raise AppError(
            "Document contains no extractable text.",
            code=ErrorCode.INVALID_INPUT,
            details={"filename": file["filename"]},
        )
    extraction_key = storage.extraction_key(file["experiment_id"], file["id"])
    storage.save_json(extraction_key, doc)
    repository.update_file_extraction(
        file["id"],
        extraction_key,
        {
            "title": doc["title"],
            "page_count": doc["page_count"],
            "document_metadata": doc.get("metadata") or {},
        },
    )
    return doc


def query_revision(revision_id: str, question: str, actor_user_id: str, retrieval_config: dict | None = None, repository=None) -> dict:
    repository = repository or RagLabRepository()
    revision = repository.get_revision(revision_id)
    if revision["status"] not in {"ready", "published"}:
        raise AppError("RAG Lab revision is not ready to query.", code=ErrorCode.CONFLICT, status_code=409)
    options = retrieval_config or revision["config"]["retrieval"]
    started = time.perf_counter()
    contexts = retrieve(
        question,
        top_k=options["top_k"],
        repository=repository.for_revision(revision_id),
        options=options,
    )
    answer = answer_with_gemini(question, contexts)
    sources = source_payload(contexts)
    confidence = retrieval_confidence(contexts)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    trial = repository.save_trial(
        {
            "revision_id": revision_id,
            "actor_user_id": actor_user_id,
            "question": question,
            "retrieval_config": options,
            "answer": answer,
            "sources": sources,
            "confidence": confidence,
            "abstained": answer_is_abstention(answer),
            "latency_ms": latency_ms,
        }
    )
    return {"trial": trial, "answer": answer, "sources": sources, "confidence": confidence, "latency_ms": latency_ms}


def create_publish_job(revision_id: str, actor_user_id: str, repository=None) -> dict:
    repository = repository or RagLabRepository()
    revision = repository.get_revision(revision_id)
    if revision["status"] != "ready":
        raise AppError("Only a ready RAG Lab revision can be published.", code=ErrorCode.CONFLICT, status_code=409)
    return IngestJobRepository(repository.client).create(
        actor_user_id,
        kind="rag_lab.publish",
        metadata={"revision_id": revision_id, "experiment_id": revision["experiment_id"]},
    )


def publish_revision(revision_id: str, actor_user_id: str | None, repository=None) -> dict:
    repository = repository or RagLabRepository()
    return repository.publish_revision(revision_id, actor_user_id)


def run_rag_lab_job(job_id: str, *, raise_on_failure: bool = False) -> None:
    jobs = IngestJobRepository()
    job = jobs.get(job_id)
    if not job:
        raise AppError("RAG Lab job not found.", code=ErrorCode.NOT_FOUND)
    jobs.update(job_id, status="running")
    try:
        if job["kind"] == "rag_lab.build":
            result = build_revision(job["metadata"]["revision_id"])
        elif job["kind"] == "rag_lab.publish":
            result = publish_revision(job["metadata"]["revision_id"], job.get("actor_user_id"))
        else:
            raise AppError("Unsupported RAG Lab job kind.", code=ErrorCode.INVALID_INPUT)
    except Exception as exc:
        jobs.update(job_id, status="failed", error=str(exc))
        if raise_on_failure:
            raise
        return
    jobs.update(job_id, status="succeeded", result=result)
