from datetime import UTC, datetime
from typing import Any

from supabase import Client

from rag_errors import RagError
from services.storage import supabase_client


class DocumentRepository:
    def __init__(self, client: Client | None = None):
        self.client = client or supabase_client()

    def indexed_sources(self) -> set[str]:
        result = self.client.table("documents").select("source").execute()
        return {row["source"] for row in result.data or [] if row.get("source")}

    def upsert_document(self, doc: dict, status: str = "indexing") -> str:
        metadata = {"ingest_status": status, "ingest_started_at": datetime.now(UTC).isoformat()}
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

    def replace_chunks(self, source: str, rows: list[dict]) -> int:
        self.client.table("document_chunks").delete().eq("source", source).execute()
        if rows:
            self.client.table("document_chunks").insert(rows).execute()
        return len(rows)

    def match_chunks(self, query_embedding: list[float], query_text: str, match_count: int) -> list[dict]:
        result = self.client.rpc(
            "match_document_chunks",
            {"query_embedding": query_embedding, "query_text": query_text, "match_count": match_count, "filter": {}},
        ).execute()
        return result.data or []


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

    def create(self, actor_user_id: str | None = None) -> dict:
        row = {"kind": "documents.ingest", "status": "queued", "actor_user_id": actor_user_id, "metadata": {}}
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
            "metadata": metadata or {},
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if status == "running":
            updates["started_at"] = datetime.now(UTC).isoformat()
        if status in {"succeeded", "failed"}:
            updates["finished_at"] = datetime.now(UTC).isoformat()
        self.client.table("ingest_jobs").update(updates).eq("id", job_id).execute()
