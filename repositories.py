import os
from datetime import UTC, datetime
from typing import Any

from supabase import Client

from rag_errors import RagError
from services.storage import supabase_client


class DocumentRepository:
    def __init__(self, client: Client | None = None):
        self.client = client or supabase_client()

    def indexed_sources(self) -> set[str]:
        result = self.client.table("documents").select("source,metadata").execute()
        current_version = index_version()
        return {
            row["source"]
            for row in result.data or []
            if row.get("source")
            and (row.get("metadata") or {}).get("index_version") == current_version
            and (row.get("metadata") or {}).get("ingest_status") == "indexed"
        }

    def list_documents(
        self,
        *,
        ingest_status: str = "indexed",
        search: str | None = None,
        kind: str | None = None,
        document_type: str | None = None,
        year: str | None = None,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        fields = "id,source,kind,title,page_count,metadata,created_at,updated_at"
        query = self.client.table("documents").select(fields, count="exact").eq(
            "metadata->>ingest_status", ingest_status
        )

        if search:
            value = postgrest_quoted_ilike(search)
            query = query.or_(f"source.ilike.{value},title.ilike.{value}")
        if kind:
            query = query.eq("kind", kind)
        if document_type:
            query = query.eq("metadata->>document_type", document_type)
        if year:
            query = query.contains("metadata", {"years": [year]})

        descending = sort_order == "desc"
        query = query.order(sort_by, desc=descending).order("id", desc=descending)
        result = query.range(offset, offset + limit - 1).execute()
        rows = result.data or []
        total = int(result.count or 0)
        return {
            "items": [document_library_item(row) for row in rows],
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total": total,
                "has_more": offset + len(rows) < total,
            },
        }

    def upsert_document(self, doc: dict, status: str = "indexing") -> str:
        metadata = {
            **(doc.get("metadata") or {}),
            "index_version": index_version(),
            "ingest_status": status,
            "ingest_started_at": datetime.now(UTC).isoformat(),
        }
        document_row = {
            "source": doc["source"],
            "kind": doc["kind"],
            "title": doc["title"],
            "page_count": doc["page_count"],
            "metadata": metadata,
        }
        result = self.client.table("documents").upsert(document_row, on_conflict="source").execute()
        return result.data[0]["id"]

    def mark_document_status(self, source: str, status: str, details: dict[str, Any] | None = None) -> None:
        metadata = {"ingest_status": status, "ingest_updated_at": datetime.now(UTC).isoformat(), **(details or {})}
        self.client.table("documents").update(
            {"metadata": metadata, "updated_at": datetime.now(UTC).isoformat()}
        ).eq("source", source).execute()

    def record_ingest_failure(self, source: str, error: str) -> None:
        """Ensure failures before document extraction are visible in the library."""
        result = self.client.table("documents").select("id,metadata").eq("source", source).limit(1).execute()
        now = datetime.now(UTC).isoformat()
        if result.data:
            metadata = {
                **(result.data[0].get("metadata") or {}),
                "ingest_status": "failed",
                "ingest_error": error,
                "ingest_updated_at": now,
            }
            self.client.table("documents").update({"metadata": metadata, "updated_at": now}).eq(
                "source", source
            ).execute()
            return

        suffix = source.rsplit(".", 1)[-1].lower() if "." in source else "document"
        self.client.table("documents").insert(
            {
                "source": source,
                "kind": suffix,
                "title": source,
                "page_count": None,
                "metadata": {
                    "ingest_status": "failed",
                    "ingest_error": error,
                    "ingest_updated_at": now,
                    "index_version": index_version(),
                },
            }
        ).execute()

    def replace_chunks(self, source: str, rows: list[dict]) -> int:
        self.delete_chunks(source)
        return self.insert_chunk_batch(rows)

    def delete_chunks(self, source: str) -> None:
        self.client.table("document_chunks").delete().eq("source", source).execute()

    def insert_chunk_batch(self, rows: list[dict]) -> int:
        if rows:
            self.client.table("document_chunks").insert(rows).execute()
        return len(rows)

    def match_chunks(self, query_embedding: list[float], query_text: str, match_count: int) -> list[dict]:
        result = self.client.rpc(
            "match_document_chunks",
            {"query_embedding": query_embedding, "query_text": query_text, "match_count": match_count, "filter": {}},
        ).execute()
        return result.data or []

    def neighbor_chunks(self, document_id: str, chunk_index: int, radius: int = 1) -> list[dict]:
        result = (
            self.client.table("document_chunks")
            .select("id,document_id,source,chunk_index,chunk_type,section_heading,page_start,page_end,content,metadata")
            .eq("document_id", document_id)
            .gte("chunk_index", max(0, chunk_index - radius))
            .lte("chunk_index", chunk_index + radius)
            .order("chunk_index")
            .execute()
        )
        return result.data or []


def index_version() -> str:
    return os.getenv("RAG_INDEX_VERSION", "3").strip() or "3"


def postgrest_quoted_ilike(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"*{escaped}*"'


def document_library_item(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") or {}
    source = row.get("source") or ""
    ingest_status = metadata.get("ingest_status") or "indexed"
    return {
        "id": row["id"],
        "filename": source,
        "title": row.get("title") or source,
        "kind": row.get("kind") or "document",
        "page_count": row.get("page_count"),
        "document_type": metadata.get("document_type") or "document",
        "authority": metadata.get("authority"),
        "years": metadata.get("years") or [],
        "chunk_count": int(metadata.get("chunks") or 0),
        "status": ingest_status,
        "ingest_error": metadata.get("ingest_error"),
        "retryable": ingest_status == "failed",
        "ingested_at": (metadata.get("ingest_updated_at") or row.get("updated_at"))
        if ingest_status == "indexed"
        else None,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


class ChatRepository:
    def __init__(self, client: Client | None = None):
        self.client = client or supabase_client()

    def create_session(self, title: str | None, user_id: str | None) -> dict:
        row = {"title": title or "New chat", "user_id": user_id, "metadata": {}}
        result = self.client.table("chat_sessions").insert(row).execute()
        return result.data[0]

    def list_sessions(self, user_id: str, limit: int = 20) -> list[dict]:
        result = (
            self.client.table("chat_sessions")
            .select("id,user_id,title,metadata,created_at,updated_at")
            .eq("user_id", user_id)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data

    def assert_session_owner(self, session_id: str, user_id: str) -> None:
        session = (
            self.client.table("chat_sessions")
            .select("id")
            .eq("id", session_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not session.data:
            raise RagError(f"Chat session not found: {session_id}")

    def get_messages(self, session_id: str, user_id: str, limit: int | None = None) -> list[dict]:
        self.assert_session_owner(session_id, user_id)
        query = (
            self.client.table("chat_messages")
            .select("id,session_id,role,content,sources,metadata,created_at")
            .eq("session_id", session_id)
            .order("created_at", desc=False)
        )
        if limit:
            query = query.limit(limit)
        return query.execute().data

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        sources: list[dict] | None = None,
        metadata: dict | None = None,
    ) -> dict:
        row = {
            "session_id": session_id,
            "role": role,
            "content": content,
            "sources": sources or [],
            "metadata": metadata or {},
        }
        result = self.client.table("chat_messages").insert(row).execute()
        self.touch_session(session_id)
        return result.data[0]

    def delete_session(self, session_id: str, user_id: str) -> dict:
        result = self.client.table("chat_sessions").delete().eq("id", session_id).eq("user_id", user_id).execute()
        if not result.data:
            raise RagError(f"Chat session not found: {session_id}")
        return result.data[0]

    def delete_message(self, session_id: str, message_id: str, user_id: str) -> dict:
        self.assert_session_owner(session_id, user_id)
        result = self.client.table("chat_messages").delete().eq("session_id", session_id).eq("id", message_id).execute()
        self.touch_session(session_id)
        if not result.data:
            raise RagError(f"Chat message not found in session {session_id}: {message_id}")
        return result.data[0]

    def touch_session(self, session_id: str) -> None:
        self.client.table("chat_sessions").update({"updated_at": datetime.now(UTC).isoformat()}).eq("id", session_id).execute()


class IngestJobRepository:
    def __init__(self, client: Client | None = None):
        self.client = client or supabase_client()

    def create(self, actor_user_id: str | None = None, *, source: str | None = None) -> dict:
        metadata = {"source": source, "scope": "document"} if source else {"scope": "corpus"}
        row = {"kind": "documents.ingest", "status": "queued", "actor_user_id": actor_user_id, "metadata": metadata}
        result = self.client.table("ingest_jobs").insert(row).execute()
        return result.data[0]

    def get(self, job_id: str) -> dict | None:
        result = self.client.table("ingest_jobs").select("*").eq("id", job_id).limit(1).execute()
        return result.data[0] if result.data else None

    def update(
        self,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        updates = {
            "status": status,
            "result": result,
            "error": error,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if metadata is not None:
            current = self.get(job_id)
            updates["metadata"] = {**((current or {}).get("metadata") or {}), **metadata}
        if status == "running":
            updates["started_at"] = datetime.now(UTC).isoformat()
            updates["finished_at"] = None
        if status == "queued":
            updates["started_at"] = None
            updates["finished_at"] = None
        if status in {"succeeded", "failed"}:
            updates["finished_at"] = datetime.now(UTC).isoformat()
        self.client.table("ingest_jobs").update(updates).eq("id", job_id).execute()
