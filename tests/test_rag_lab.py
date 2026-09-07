from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import rag_lab_service
import routers.rag_lab as rag_lab_router
from app import app
from auth import CurrentUser, get_current_user
from chunking import chunk_document
from schemas import RagLabChunkingConfig

client = TestClient(app)


def test_chunking_config_rejects_overlap_equal_to_chunk_size():
    with pytest.raises(ValidationError):
        RagLabChunkingConfig(max_tokens=200, overlap_tokens=200)


def test_chunk_document_accepts_explicit_profile():
    doc = {
        "source": "questions.txt",
        "kind": "txt",
        "title": "Questions",
        "metadata": {},
        "pages": [{"page": None, "text": "1. What is forestry?\nAnswer: The management of forests."}],
    }

    chunks = chunk_document(doc, max_tokens=200, overlap_tokens=0, profile="faq")

    assert chunks[0]["chunk_type"] == "faq"
    assert chunks[0]["metadata"]["profile"] == "faq"


def test_upload_files_uses_experiment_scoped_generated_storage_key():
    class Repository:
        def get_experiment(self, _experiment_id):
            return {"status": "draft"}

        def list_files(self, _experiment_id):
            return []

        def add_file(self, row):
            return row

        def update_experiment(self, _experiment_id, _updates):
            pass

    class Storage:
        def file_key(self, experiment_id, file_id, filename):
            return f"rag-lab/{experiment_id}/files/{file_id}/{filename}"

        def save_bytes(self, key, content, content_type):
            self.saved = (key, content, content_type)

    storage = Storage()
    [result] = rag_lab_service.upload_files(
        "experiment-1",
        [SimpleNamespace(filename="rules.txt", content=b"rules", content_type="text/plain")],
        repository=Repository(),
        storage=storage,
    )

    assert result["storage_key"].startswith("rag-lab/experiment-1/files/")
    assert result["storage_key"].endswith("/rules.txt")
    assert result["checksum_sha256"]
    assert storage.saved[1] == b"rules"


def test_extracted_document_reuses_cached_extraction():
    class Storage:
        def load_json(self, key):
            assert key == "cached.json"
            return {"source": "rules.txt", "pages": []}

    result = rag_lab_service.extracted_document(
        {"extraction_key": "cached.json"},
        Storage(),
        SimpleNamespace(),
    )

    assert result["source"] == "rules.txt"


def test_insert_embedded_batch_uses_one_embedding_request(monkeypatch):
    class Repository:
        def insert_chunk_batch(self, rows):
            self.rows = rows
            return len(rows)

    repository = Repository()
    calls = []

    def embed_texts(texts):
        calls.append(texts)
        return [[1.0, 0.0], [0.0, 1.0]]

    monkeypatch.setattr(rag_lab_service, "embed_texts", embed_texts)

    inserted = rag_lab_service.insert_embedded_batch(
        repository,
        [({"content": "first"}, "first context"), ({"content": "second"}, "second context")],
    )

    assert inserted == 2
    assert calls == [["first context", "second context"]]
    assert repository.rows[0]["embedding"] == [1.0, 0.0]


def test_failed_revision_resumes_existing_chunks(monkeypatch):
    class Repository:
        def __init__(self):
            self.deleted = False
            self.inserted_rows = []
            self.revision_updates = []

        def get_revision(self, _revision_id):
            return {
                "status": "failed",
                "experiment_id": "experiment-1",
                "config": {"chunking": {"max_tokens": 200, "overlap_tokens": 0, "profile": "auto"}},
            }

        def list_files(self, _experiment_id):
            return [{"id": "file-1", "filename": "rules.txt"}]

        def existing_chunk_keys(self, _revision_id):
            return {("file-1", 0)}

        def delete_revision_chunks(self, _revision_id):
            self.deleted = True

        def update_revision(self, _revision_id, **updates):
            self.revision_updates.append(updates)

        def update_experiment(self, _experiment_id, _updates):
            pass

        def insert_chunk_batch(self, rows):
            self.inserted_rows.extend(rows)
            return len(rows)

    chunks = [
        {
            "source": "rules.txt",
            "chunk_index": index,
            "chunk_type": "text",
            "section_heading": None,
            "page_start": None,
            "page_end": None,
            "content": f"chunk {index}",
            "token_estimate": 2,
            "metadata": {},
        }
        for index in range(2)
    ]
    repository = Repository()
    monkeypatch.setattr(rag_lab_service, "extracted_document", lambda *_args: {"pages": []})
    monkeypatch.setattr(rag_lab_service, "iter_document_chunks", lambda *_args, **_kwargs: iter(chunks))
    monkeypatch.setattr(rag_lab_service, "embed_texts", lambda texts: [[1.0, 0.0] for _ in texts])

    result = rag_lab_service.build_revision("revision-1", repository=repository, storage=SimpleNamespace())

    assert repository.deleted is False
    assert [row["chunk_index"] for row in repository.inserted_rows] == [1]
    assert result["chunks"] == 2
    assert repository.revision_updates[-1] == {"status": "ready", "chunk_count": 2}


def test_rag_lab_routes_require_admin():
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id="manager-1", email="manager@example.com", role="knowledge_manager")
    try:
        response = client.get("/admin/rag-lab/experiments")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


def test_admin_can_create_rag_lab_experiment(monkeypatch):
    class Repository:
        def create_experiment(self, name, description, config, owner_user_id):
            return {"id": "experiment-1", "name": name, "description": description, "config": config, "owner_user_id": owner_user_id}

    monkeypatch.setattr(rag_lab_router, "RagLabRepository", Repository)
    monkeypatch.setattr(rag_lab_router, "audit_event", lambda *_args, **_kwargs: None)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id="admin-1", email="admin@example.com", role="admin")
    try:
        response = client.post("/admin/rag-lab/experiments", json={"name": "Forestry policies"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["experiment"]["config"]["chunking"]["max_tokens"] == 600
    assert response.json()["experiment"]["owner_user_id"] == "admin-1"
