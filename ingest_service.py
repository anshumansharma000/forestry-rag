from chunking import chunk_document
from documents import load_documents
from repositories import DocumentRepository, IngestJobRepository
from retrieval import chunk_row


def build_index(repository: DocumentRepository | None = None) -> dict:
    repository = repository or DocumentRepository()
    docs = load_documents()
    existing_sources = repository.indexed_sources()
    documents_added = 0
    documents_skipped = 0
    chunks_added = 0

    for doc in docs:
        if doc["source"] in existing_sources:
            documents_skipped += 1
            continue

        document_id = repository.upsert_document(doc, status="indexing")
        chunks = chunk_document(doc)
        try:
            rows = [chunk_row(document_id, chunk) for chunk in chunks]
            chunks_added += repository.replace_chunks(doc["source"], rows)
            repository.mark_document_status(doc["source"], "indexed", {"chunks": len(rows)})
        except Exception:
            repository.mark_document_status(doc["source"], "failed")
            raise

        documents_added += 1
        existing_sources.add(doc["source"])

    return {
        "documents": len(docs),
        "documents_added": documents_added,
        "documents_skipped": documents_skipped,
        "chunks": chunks_added,
        "chunks_added": chunks_added,
        "storage": "supabase_pgvector",
    }


def preview_chunks() -> dict:
    docs = load_documents()
    chunks = []
    for doc in docs:
        chunks.extend(chunk_document(doc))
    return {"documents": len(docs), "chunks": chunks}


def create_ingest_job(actor_user_id: str | None = None, repository: IngestJobRepository | None = None) -> dict:
    repository = repository or IngestJobRepository()
    return repository.create(actor_user_id)


def get_ingest_job(job_id: str, repository: IngestJobRepository | None = None) -> dict | None:
    repository = repository or IngestJobRepository()
    return repository.get(job_id)


def run_ingest_job(job_id: str, repository: IngestJobRepository | None = None) -> None:
    repository = repository or IngestJobRepository()
    repository.update(job_id, status="running")
    try:
        result = build_index()
    except Exception as exc:
        repository.update(job_id, status="failed", error=str(exc))
        return
    repository.update(job_id, status="succeeded", result=result)
