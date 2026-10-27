import os
from datetime import UTC, datetime
from typing import Any

from supabase import Client

from consistency import DatabaseLease
from errors import AppError, ErrorCode
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

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        result = self.client.table("documents").select("id,source,kind,title,metadata").eq("id", document_id).limit(1).execute()
        return result.data[0] if result.data else None

    def preview_chunks(self, document_id: str, offset: int, limit: int) -> list[dict[str, Any]]:
        # Only published chunks; never return embeddings or revision staging.
        result = (self.client.table("document_chunks")
                  .select("chunk_index,section_heading,page_start,page_end,content")
                  .eq("document_id", document_id).order("chunk_index")
                  .range(offset, offset + limit).execute())
        return result.data or []

    def prepare_delete(self, document_id: str, token: str) -> dict:
        try:
            result = self.client.rpc("prepare_document_delete", {"p_id": document_id, "p_token": token}).execute().data
        except Exception as exc:
            if getattr(exc, "code", None) == "PGRST202":
                raise AppError(
                    "Document deletion is not set up in the database. Apply database migration 017 and retry.",
                    code=ErrorCode.CONFIG_ERROR, status_code=503,
                    details={"migration": "017_admin_document_deletion.sql"},
                ) from exc
            raise
        if result.get("status") == "missing":
            raise AppError("Document not found.", code=ErrorCode.NOT_FOUND, status_code=404)
        if result.get("status") == "busy":
            raise AppError("This document or the library is queued or processing. Try again when processing finishes.",
                           code=ErrorCode.CONFLICT, status_code=409)
        return result

    def finish_delete(self, document_id: str, token: str, audit: dict) -> None:
        self.client.rpc("finish_document_delete", {"p_id": document_id, "p_token": token, "p_audit": audit}).execute()

    def upload_state(self, source: str) -> str:
        """Only a source with no published index can be replaced after confirmation."""
        rows = self.client.table("documents").select("id,metadata").eq("source", source).limit(1).execute().data or []
        metadata = (rows[0].get("metadata") or {}) if rows else {}
        if metadata.get("ingest_status") == "indexed":
            return "indexed"
        if rows and self.client.table("document_chunks").select("id").eq("document_id", rows[0]["id"]).limit(1).execute().data:
            return "indexed"
        # Filenames have already passed safe_filename (no quotes or commas).
        active = (self.client.table("ingest_jobs").select("id").eq("kind", "documents.ingest")
                  .in_("status", ["queued", "running"])
                  .or_(f'metadata->>source.eq."{source}",metadata->>scope.eq.corpus').limit(1).execute().data)
        if active:
            return "processing"
        return "failed" if metadata.get("ingest_status") == "failed" else "unindexed"

    def document_ids_by_sources(self, sources: set[str]) -> dict[str, str]:
        if not sources:
            return {}
        result = self.client.table("documents").select("id,source").in_("source", sorted(sources)).execute()
        return {row["source"]: str(row["id"]) for row in result.data or [] if row.get("source") and row.get("id")}

    def categorization_for_source(self, source: str) -> dict:
        rows = self.client.table("documents").select("metadata").eq("source", source).limit(1).execute().data or []
        if not rows:
            return {}
        return (rows[0].get("metadata") or {}).get("categorization") or {}

    def save_categorization(self, source: str, patch: dict, user_id: str) -> dict:
        from legal_registry import legal_namespace

        # Preserve the existing independent write gate for legal annotations.
        namespace = legal_namespace() if os.getenv("LEGAL_REGISTRY_WRITES_ENABLED", "false").lower() == "true" else None
        return self.client.rpc("save_document_categorization", {
            "p_source": source, "p_patch": patch, "p_updated_by": user_id, "p_namespace": namespace,
        }).execute().data

    def check_categorization_support(self) -> None:
        from legal_registry import legal_namespace

        if os.getenv("LEGAL_REGISTRY_WRITES_ENABLED", "false").lower() == "true":
            legal_namespace()
        try:
            self.client.rpc("document_categorization_ready", {}).execute()
        except Exception as exc:
            if getattr(exc, "code", None) not in ("PGRST202", "42883"):
                raise
            raise AppError(
                "Apply migration 016_document_categorization.sql before uploading categorization metadata.",
                code=ErrorCode.CONFIG_ERROR,
            ) from exc

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
        category: str | None = None,
    ) -> dict[str, Any]:
        fields = "id,source,kind,title,page_count,metadata,created_at,updated_at"
        query = self.client.table("documents").select(fields, count="exact").eq("metadata->>ingest_status", ingest_status)

        if search:
            value = postgrest_quoted_ilike(search)
            query = query.or_(f"source.ilike.{value},title.ilike.{value}")
        if kind:
            query = query.eq("kind", kind)
        if document_type:
            query = query.eq("metadata->>document_type", document_type)
        if category:
            query = query.eq("metadata->categorization->values->>document_type", category)
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

    def index_lease(self):
        return DatabaseLease(self.client, "documents:ingest")

    def begin_revision(self, doc: dict) -> dict:
        snapshot = {key: doc.get(key) for key in ("source", "kind", "title", "page_count")}
        snapshot["metadata"] = {**(doc.get("metadata") or {}), "index_version": index_version()}
        if "_expected_categorization" in doc:
            try:
                return self.client.rpc("begin_categorized_revision", {
                    "p_document": snapshot, "p_token": doc["_lease_token"],
                    "p_expected_categorization": doc["_expected_categorization"],
                }).execute().data
            except Exception as exc:
                # Older deployments keep ingesting uncategorized files until the
                # additive migration is applied. Never hide an actual DB failure.
                existing = doc["_expected_categorization"]
                if (
                    existing.get("overrides") or existing.get("registry_namespace")
                    or getattr(exc, "code", None) not in ("PGRST202", "42883")
                ):
                    raise
        return self.client.rpc("begin_consistent_revision", {"p_document": snapshot, "p_token": doc["_lease_token"]}).execute().data

    def insert_revision_chunks(self, rows: list[dict]) -> int:
        if rows:
            self.client.table("document_revision_chunks").insert(rows).execute()
        return len(rows)

    def publish_revision(self, revision_id: str, expected_chunks: int, *, token: str) -> dict:
        return (
            self.client.rpc(
                "publish_consistent_revision",
                {
                    "p_revision_id": revision_id,
                    "p_expected_chunks": expected_chunks,
                    "p_token": token,
                },
            )
            .execute()
            .data
        )

    def fail_revision(self, revision_id: str, error: str, *, token: str) -> None:
        self.client.rpc(
            "fail_consistent_revision",
            {
                "p_revision_id": revision_id,
                "p_error": error,
                "p_token": token,
            },
        ).execute()

    def record_ingest_failure(self, source: str, error: str, *, token: str) -> None:
        # A failed refresh must never hide the last successfully published index.
        self.client.rpc(
            "record_consistent_ingest_failure",
            {
                "p_source": source,
                "p_error": error,
                "p_token": token,
            },
        ).execute()

    def parallel_clone(self):
        from services.storage import thread_supabase_client

        return DocumentRepository(thread_supabase_client())

    def cache_versions(self, namespace="", revision=""):
        return self.client.rpc("answer_cache_versions", {"p_namespace": namespace, "p_revision": revision or None}).execute().data

    def neighbor_chunks_many(self, anchors):
        payload = [
            {
                "anchor_id": str(a["id"]),
                "document_id": str(a["document_id"]),
                "chunk_index": a["chunk_index"],
                "revision_id": (a.get("metadata") or {}).get("index_revision_id"),
            }
            for a in anchors
        ]
        try:
            rows = self.client.rpc("document_chunk_neighbors_batch", {"p_anchors": payload}).execute().data or []
        except Exception as exc:
            # Only a missing additive RPC permits fallback; do not mask outages.
            if getattr(exc, "code", None) not in ("PGRST202", "42883"):
                raise
            return {
                str(a["id"]): self.neighbor_chunks(
                    a["document_id"],
                    a["chunk_index"],
                    radius=1,
                    **({"revision_id": a["metadata"]["index_revision_id"]} if a.get("metadata", {}).get("index_revision_id") else {}),
                )
                for a in anchors
            }
        return {
            str(a["id"]): [{k: v for k, v in r.items() if k != "anchor_id"} for r in rows if r["anchor_id"] == str(a["id"])]
            for a in anchors
        }

    def match_chunks(self, query_embedding: list[float], query_text: str, match_count: int) -> list[dict]:
        result = self.client.rpc(
            "match_document_chunks",
            {"query_embedding": query_embedding, "query_text": query_text, "match_count": match_count, "filter": {}},
        ).execute()
        return result.data or []

    def neighbor_chunks(self, document_id: str, chunk_index: int, radius: int = 1, revision_id: str | None = None) -> list[dict]:
        result = self.client.rpc(
            "document_chunk_neighbors",
            {
                "p_document_id": document_id,
                "p_chunk_index": chunk_index,
                "p_radius": radius,
                "p_revision_id": revision_id,
            },
        ).execute()
        return result.data or []


def index_version() -> str:
    return os.getenv("RAG_INDEX_VERSION", "4").strip() or "4"


def postgrest_quoted_ilike(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"*{escaped}*"'


def document_library_item(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") or {}
    source = row.get("source") or ""
    ingest_status = metadata.get("ingest_status") or "indexed"
    item = {
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
        "ingested_at": (metadata.get("ingest_updated_at") or row.get("updated_at")) if ingest_status == "indexed" else None,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    if metadata.get("categorization"):
        item["categorization"] = metadata["categorization"]
    return item


class ChatRepository:
    def __init__(self, client: Client | None = None):
        self.client = client or supabase_client()

    def begin_turn(self, session_id, user_id, request_id, request, token):
        return (
            self.client.rpc(
                "begin_chat_turn",
                {
                    "p_session": session_id,
                    "p_user": user_id,
                    "p_request_id": request_id,
                    "p_request": request,
                    "p_token": token,
                },
            )
            .execute()
            .data
        )

    def turn_lease(self, session_id, token):
        return DatabaseLease(self.client, "chat:" + session_id, token=token, claimed=True)

    def complete_turn(self, session_id, user_id, request_id, token, response):
        return (
            self.client.rpc(
                "complete_chat_turn",
                {
                    "p_session": session_id,
                    "p_user": user_id,
                    "p_request_id": request_id,
                    "p_token": token,
                    "p_response": response,
                },
            )
            .execute()
            .data
        )

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
        session = self.client.table("chat_sessions").select("id").eq("id", session_id).eq("user_id", user_id).limit(1).execute()
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

    def get_recent_messages_for_answer(self, session_id: str, user_id: str, limit: int = 10) -> list[dict]:
        self.assert_session_owner(session_id, user_id)
        rows = (
            self.client.table("chat_messages")
            .select("id,role,content,metadata,created_at")
            .eq("session_id", session_id)
            .order("created_at", desc=True)
            .order("id", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )
        return list(reversed(rows))

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

    def create(
        self,
        actor_user_id: str | None = None,
        *,
        source: str | None = None,
        kind: str = "documents.ingest",
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        job_metadata = metadata or ({"source": source, "scope": "document"} if source else {"scope": "corpus"})
        row = {"kind": kind, "status": "queued", "actor_user_id": actor_user_id, "metadata": job_metadata}
        result = self.client.table("ingest_jobs").insert(row).execute()
        return result.data[0]

    def get(self, job_id: str) -> dict | None:
        result = self.client.table("ingest_jobs").select("*").eq("id", job_id).limit(1).execute()
        return result.data[0] if result.data else None

    def lease(self, job_id):
        return DatabaseLease(self.client, "job:" + job_id)

    def recover(self):
        return self.client.rpc("recover_ingest_jobs").execute().data or []

    def update(
        self,
        job_id: str,
        *,
        status: str,
        result: dict | None = None,
        error: str | None = None,
        metadata: dict | None = None,
        token: str | None = None,
    ) -> dict | None:
        return (
            self.client.rpc(
                "transition_ingest_job",
                {
                    "p_id": job_id,
                    "p_status": status,
                    "p_token": token,
                    "p_result": result,
                    "p_error": error,
                    "p_metadata": metadata or {},
                },
            )
            .execute()
            .data
        )
