import os

from chunking import chunk_document, iter_document_chunks
from documents import iter_documents
from errors import AppError, ErrorCode
from repositories import DocumentRepository, IngestJobRepository, index_version
from retrieval import chunk_row


def build_index(repository: DocumentRepository | None = None, *, source: str | None = None) -> dict:
    repository = repository or DocumentRepository()
    existing_sources = repository.indexed_sources()
    if source and source in existing_sources:
        return {
            "documents": 1,
            "documents_added": 0,
            "documents_skipped": 1,
            "chunks": 0,
            "chunks_added": 0,
            "source": source,
            "storage": "supabase_pgvector",
        }

    documents_seen = 0
    documents_added = 0
    documents_skipped = 0
    chunks_added = 0

    for doc in iter_documents(source=source):
        documents_seen += 1
        if doc["source"] in existing_sources:
            documents_skipped += 1
            continue

        document_id = repository.upsert_document(doc, status="indexing")
        index_metadata = {**(doc.get("metadata") or {}), "index_version": index_version()}
        try:
            document_chunks = persist_document_chunks(repository, document_id, doc)
            chunks_added += document_chunks
            repository.mark_document_status(doc["source"], "indexed", {**index_metadata, "chunks": document_chunks})
        except Exception:
            try:
                repository.delete_chunks(doc["source"])
            except Exception:
                pass
            repository.mark_document_status(doc["source"], "failed", index_metadata)
            raise

        documents_added += 1
        existing_sources.add(doc["source"])

    if source and documents_seen == 0:
        raise AppError(
            "Document was not found or contains no extractable text.",
            code=ErrorCode.INVALID_INPUT,
            details={"source": source},
        )

    return {
        "documents": documents_seen,
        "documents_added": documents_added,
        "documents_skipped": documents_skipped,
        "chunks": chunks_added,
        "chunks_added": chunks_added,
        "source": source,
        "storage": "supabase_pgvector",
    }


def persist_document_chunks(repository: DocumentRepository, document_id: str, doc: dict) -> int:
    batch_size = positive_env_int("INGEST_BATCH_SIZE", 24)
    max_chunks = positive_env_int("MAX_DOCUMENT_CHUNKS", 3000)
    source = doc["source"]
    repository.delete_chunks(source)
    batch = []
    inserted = 0

    for chunk in iter_document_chunks(doc):
        if inserted + len(batch) >= max_chunks:
            raise AppError(
                "Document produced too many chunks.",
                code=ErrorCode.INVALID_INPUT,
                details={"source": source, "max_chunks": max_chunks},
            )
        batch.append(chunk_row(document_id, chunk))
        if len(batch) >= batch_size:
            inserted += repository.insert_chunk_batch(batch)
            batch.clear()

    if batch:
        inserted += repository.insert_chunk_batch(batch)
    return inserted


def positive_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise AppError(f"{name} must be an integer.", code=ErrorCode.CONFIG_ERROR) from exc
    if value <= 0:
        raise AppError(f"{name} must be greater than 0.", code=ErrorCode.CONFIG_ERROR)
    return value


def preview_chunks(
    *,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_content: bool = False,
    max_content_chars: int = 500,
    all_sources: bool = False,
) -> dict:
    if source and all_sources:
        raise AppError(
            "Use either source or all_sources, not both.",
            code=ErrorCode.INVALID_INPUT,
            details={"source": source, "all_sources": all_sources},
        )
    if not source and not all_sources:
        raise AppError(
            "Chunk preview requires a source filename. Set all_sources=true only for advanced corpus-wide debugging.",
            code=ErrorCode.INVALID_INPUT,
            details={"required": "source", "advanced_override": "all_sources=true"},
        )

    chunks = []
    documents_processed = 0
    chunks_seen = 0
    target_count = offset + limit + 1

    for doc in iter_documents(source=source):
        documents_processed += 1
        remaining_to_probe = target_count - chunks_seen
        if remaining_to_probe <= 0:
            break

        for chunk in chunk_document(doc, max_chunks=remaining_to_probe):
            chunks_seen += 1
            if chunks_seen <= offset:
                continue
            if len(chunks) >= limit:
                return chunk_preview_response(
                    documents_processed,
                    chunks,
                    chunks_seen=chunks_seen,
                    offset=offset,
                    limit=limit,
                    has_more=True,
                    source=source,
                    all_sources=all_sources,
                    include_content=include_content,
                    max_content_chars=max_content_chars,
                )
            chunks.append(preview_chunk(chunk, include_content=include_content, max_content_chars=max_content_chars))

    return chunk_preview_response(
        documents_processed,
        chunks,
        chunks_seen=chunks_seen,
        offset=offset,
        limit=limit,
        has_more=False,
        source=source,
        all_sources=all_sources,
        include_content=include_content,
        max_content_chars=max_content_chars,
    )


def preview_chunk(chunk: dict, *, include_content: bool, max_content_chars: int) -> dict:
    content = chunk.get("content") or ""
    preview = {key: value for key, value in chunk.items() if key != "content"}
    if include_content:
        preview["content"] = content[:max_content_chars] if max_content_chars else ""
        preview["content_truncated"] = len(content) > max_content_chars
    else:
        preview["content"] = ""
        preview["content_omitted"] = True
    preview["content_chars"] = len(content)
    return preview


def chunk_preview_response(
    documents_processed: int,
    chunks: list[dict],
    *,
    chunks_seen: int,
    offset: int,
    limit: int,
    has_more: bool,
    source: str | None,
    all_sources: bool,
    include_content: bool,
    max_content_chars: int,
) -> dict:
    return {
        "documents": documents_processed,
        "documents_processed": documents_processed,
        "chunks": chunks,
        "chunks_returned": len(chunks),
        "chunks_seen": chunks_seen,
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "source": source,
        "all_sources": all_sources,
        "include_content": include_content,
        "max_content_chars": max_content_chars,
    }


def create_ingest_job(
    actor_user_id: str | None = None,
    *,
    source: str | None = None,
    repository: IngestJobRepository | None = None,
) -> dict:
    repository = repository or IngestJobRepository()
    return repository.create(actor_user_id, source=source)


def get_ingest_job(job_id: str, repository: IngestJobRepository | None = None) -> dict | None:
    repository = repository or IngestJobRepository()
    return repository.get(job_id)


def mark_ingest_job_enqueued(
    job_id: str,
    *,
    task_id: str,
    queue: str = "celery",
    repository: IngestJobRepository | None = None,
) -> None:
    repository = repository or IngestJobRepository()
    task_id_key = "celery_task_id" if queue == "celery" else "local_task_id"
    repository.update(job_id, status="queued", metadata={"queue": queue, task_id_key: task_id})


def mark_ingest_job_enqueue_failed(
    job_id: str,
    *,
    error: str,
    queue: str = "celery",
    repository: IngestJobRepository | None = None,
) -> None:
    repository = repository or IngestJobRepository()
    repository.update(job_id, status="failed", error=error, metadata={"queue": queue})


def run_ingest_job(job_id: str, repository: IngestJobRepository | None = None, *, raise_on_failure: bool = False) -> None:
    repository = repository or IngestJobRepository()
    job = repository.get(job_id)
    source = ((job or {}).get("metadata") or {}).get("source")
    repository.update(job_id, status="running")
    try:
        result = build_index(source=source)
    except Exception as exc:
        repository.update(job_id, status="failed", error=str(exc))
        if raise_on_failure:
            raise
        return
    repository.update(job_id, status="succeeded", result=result)
