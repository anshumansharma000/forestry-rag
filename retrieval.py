import os

from repositories import DocumentRepository
from services.gemini import gemini_client


def normalize_embedding(values: list[float]) -> list[float]:
    magnitude = sum(value * value for value in values) ** 0.5
    if magnitude == 0:
        return values
    return [value / magnitude for value in values]


def embed_text(text: str) -> list[float]:
    return normalize_embedding(gemini_client.embed(text, "RETRIEVAL_DOCUMENT"))


def embed_query(text: str) -> list[float]:
    return normalize_embedding(gemini_client.embed(text, "RETRIEVAL_QUERY"))


def retrieve(question: str, top_k: int | None = None, repository: DocumentRepository | None = None) -> list[dict]:
    repository = repository or DocumentRepository()
    k = top_k or int(os.getenv("TOP_K", "3"))
    query_embedding = embed_query(question)
    rows = repository.match_chunks(query_embedding, k)

    return [
        {
            "id": row["id"],
            "document_id": row["document_id"],
            "source": row["source"],
            "chunk_index": row["chunk_index"],
            "chunk_type": row["chunk_type"],
            "section_heading": row["section_heading"],
            "page_start": row["page_start"],
            "page_end": row["page_end"],
            "text": row["content"],
            "metadata": row["metadata"],
            "score": row["similarity"],
        }
        for row in rows
    ]


def format_source(record: dict) -> str:
    page_start = record.get("page_start")
    page_end = record.get("page_end")
    if page_start is None:
        return record["source"]
    if page_start == page_end:
        return f"{record['source']}, page {page_start}"
    return f"{record['source']}, pages {page_start}-{page_end}"


def source_payload(contexts: list[dict]) -> list[dict]:
    return [
        {
            "source": ctx["source"],
            "display_source": format_source(ctx),
            "page_start": ctx.get("page_start"),
            "page_end": ctx.get("page_end"),
            "chunk_index": ctx["chunk_index"],
            "score": round(ctx["score"], 4),
            "text": ctx["text"],
        }
        for ctx in contexts
    ]


def chunk_row(document_id: str, chunk: dict) -> dict:
    return {
        "document_id": document_id,
        "source": chunk["source"],
        "chunk_index": chunk["chunk_index"],
        "chunk_type": chunk["chunk_type"],
        "section_heading": chunk["section_heading"],
        "page_start": chunk["page_start"],
        "page_end": chunk["page_end"],
        "content": chunk["content"],
        "token_estimate": chunk["token_estimate"],
        "metadata": chunk["metadata"],
        "embedding": embed_text(chunk["content"]),
    }


def generate_with_gemini(prompt: str) -> str:
    return gemini_client.generate(prompt)
