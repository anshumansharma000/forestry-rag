from datetime import UTC, datetime
from typing import Any

from errors import AppError, ErrorCode
from services.storage import supabase_client


class RagLabRepository:
    def __init__(self, client=None) -> None:
        self.client = client or supabase_client()

    def create_experiment(self, name: str, description: str | None, config: dict, owner_user_id: str) -> dict:
        row = {
            "name": name,
            "description": description,
            "config": config,
            "owner_user_id": owner_user_id,
            "status": "draft",
        }
        return self.client.table("rag_lab_experiments").insert(row).execute().data[0]

    def list_experiments(self, limit: int = 50, offset: int = 0) -> dict:
        result = (
            self.client.table("rag_lab_experiments")
            .select("*", count="exact")
            .order("updated_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        rows = result.data or []
        return {
            "items": rows,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total": int(result.count or 0),
                "has_more": offset + len(rows) < int(result.count or 0),
            },
        }

    def get_experiment(self, experiment_id: str) -> dict:
        result = self.client.table("rag_lab_experiments").select("*").eq("id", experiment_id).limit(1).execute()
        if not result.data:
            raise AppError("RAG Lab experiment not found.", code=ErrorCode.NOT_FOUND, status_code=404)
        return result.data[0]

    def update_experiment(self, experiment_id: str, updates: dict) -> dict:
        self.get_experiment(experiment_id)
        updates = {**updates, "updated_at": now_iso()}
        result = self.client.table("rag_lab_experiments").update(updates).eq("id", experiment_id).execute()
        return result.data[0]

    def add_file(self, row: dict) -> dict:
        return self.client.table("rag_lab_files").insert(row).execute().data[0]

    def list_files(self, experiment_id: str) -> list[dict]:
        return (
            self.client.table("rag_lab_files")
            .select("*")
            .eq("experiment_id", experiment_id)
            .order("created_at")
            .execute()
            .data
            or []
        )

    def update_file_extraction(self, file_id: str, extraction_key: str, metadata: dict) -> None:
        self.client.table("rag_lab_files").update(
            {"extraction_key": extraction_key, "extraction_metadata": metadata, "updated_at": now_iso()}
        ).eq("id", file_id).execute()

    def create_revision(self, experiment_id: str, config: dict, created_by: str) -> dict:
        self.get_experiment(experiment_id)
        latest = (
            self.client.table("rag_lab_revisions")
            .select("revision_number")
            .eq("experiment_id", experiment_id)
            .order("revision_number", desc=True)
            .limit(1)
            .execute()
        )
        revision_number = int(latest.data[0]["revision_number"]) + 1 if latest.data else 1
        row = {
            "experiment_id": experiment_id,
            "revision_number": revision_number,
            "status": "queued",
            "config": config,
            "created_by": created_by,
        }
        revision = self.client.table("rag_lab_revisions").insert(row).execute().data[0]
        self.update_experiment(experiment_id, {"status": "building", "config": config})
        return revision

    def get_revision(self, revision_id: str) -> dict:
        result = self.client.table("rag_lab_revisions").select("*").eq("id", revision_id).limit(1).execute()
        if not result.data:
            raise AppError("RAG Lab revision not found.", code=ErrorCode.NOT_FOUND, status_code=404)
        return result.data[0]

    def list_revisions(self, experiment_id: str) -> list[dict]:
        return (
            self.client.table("rag_lab_revisions")
            .select("*")
            .eq("experiment_id", experiment_id)
            .order("revision_number", desc=True)
            .execute()
            .data
            or []
        )

    def update_revision(self, revision_id: str, *, status: str, chunk_count: int | None = None, error: str | None = None) -> None:
        updates: dict[str, Any] = {"status": status, "error": error, "updated_at": now_iso()}
        if chunk_count is not None:
            updates["chunk_count"] = chunk_count
        self.client.table("rag_lab_revisions").update(updates).eq("id", revision_id).execute()

    def delete_revision_chunks(self, revision_id: str) -> None:
        self.client.table("rag_lab_chunks").delete().eq("revision_id", revision_id).execute()

    def existing_chunk_keys(self, revision_id: str) -> set[tuple[str, int]]:
        keys: set[tuple[str, int]] = set()
        offset = 0
        page_size = 1000
        while True:
            result = (
                self.client.table("rag_lab_chunks")
                .select("file_id,chunk_index")
                .eq("revision_id", revision_id)
                .range(offset, offset + page_size - 1)
                .execute()
            )
            rows = result.data or []
            keys.update((row["file_id"], int(row["chunk_index"])) for row in rows)
            if len(rows) < page_size:
                return keys
            offset += page_size

    def insert_chunk_batch(self, rows: list[dict]) -> int:
        if rows:
            self.client.table("rag_lab_chunks").insert(rows).execute()
        return len(rows)

    def list_chunks(self, revision_id: str, offset: int, limit: int, include_content: bool) -> dict:
        fields = "id,revision_id,file_id,source,chunk_index,chunk_type,section_heading,page_start,page_end,token_estimate,metadata"
        if include_content:
            fields += ",content"
        result = (
            self.client.table("rag_lab_chunks")
            .select(fields, count="exact")
            .eq("revision_id", revision_id)
            .order("source")
            .order("chunk_index")
            .range(offset, offset + limit - 1)
            .execute()
        )
        rows = result.data or []
        total = int(result.count or 0)
        return {
            "items": rows,
            "pagination": {"offset": offset, "limit": limit, "total": total, "has_more": offset + len(rows) < total},
        }

    def match_chunks(self, query_embedding: list[float], query_text: str, match_count: int) -> list[dict]:
        result = self.client.rpc(
            "match_rag_lab_chunks",
            {
                "query_embedding": query_embedding,
                "query_text": query_text,
                "match_count": match_count,
                "target_revision_id": self.revision_id,
                "vector_candidate_count": match_count,
                "text_candidate_count": match_count,
            },
        ).execute()
        return result.data or []

    def for_revision(self, revision_id: str):
        self.revision_id = revision_id
        return self

    def neighbor_chunks(self, file_id: str, chunk_index: int, radius: int = 1) -> list[dict]:
        rows = (
            self.client.table("rag_lab_chunks")
            .select("id,file_id,source,chunk_index,chunk_type,section_heading,page_start,page_end,content,metadata")
            .eq("revision_id", self.revision_id)
            .eq("file_id", file_id)
            .gte("chunk_index", max(0, chunk_index - radius))
            .lte("chunk_index", chunk_index + radius)
            .order("chunk_index")
            .execute()
            .data
            or []
        )
        return [{**row, "document_id": row["file_id"]} for row in rows]

    def save_trial(self, row: dict) -> dict:
        return self.client.table("rag_lab_query_trials").insert(row).execute().data[0]

    def list_trials(self, revision_id: str, limit: int = 50) -> list[dict]:
        return (
            self.client.table("rag_lab_query_trials")
            .select("*")
            .eq("revision_id", revision_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )

    def publish_revision(self, revision_id: str, published_by: str | None) -> dict:
        result = self.client.rpc(
            "publish_rag_lab_revision", {"p_revision_id": revision_id, "p_published_by": published_by}
        ).execute()
        return result.data or {}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
