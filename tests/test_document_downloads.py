import pytest
from fastapi.testclient import TestClient

import auth
import routers.documents as documents_router
import services.document_storage as storage_module
from app import app
from auth import AuthError, CurrentUser, get_authenticated_user
from chat_service import enrich_legacy_citations
from retrieval import source_payload
from services.document_storage import DocumentDownload, LocalDocumentStorage

client = TestClient(app)


class Repository:
    document = None

    def get_document(self, _document_id):
        return self.document


class Storage:
    download = None

    def open_download(self, _filename, _content_type):
        return self.download


def user(role="admin"):
    return CurrentUser(id=f"{role}-1", email=f"{role}@example.com", role=role)


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("rules.pdf", "application/pdf"),
        ("rules.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("rules.txt", "text/plain; charset=utf-8"),
        ("rules.ppt", "application/vnd.ms-powerpoint"),
        ("rules.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ],
)
def test_admin_downloads_byte_exact_original_with_safe_headers(monkeypatch, filename, content_type):
    content = b"original\x00bytes"
    Repository.document = {"id": "document-1", "source": filename, "kind": filename.rsplit(".", 1)[-1], "metadata": {}}
    Storage.download = DocumentDownload(filename, content_type, len(content), iter([content[:5], content[5:]]))
    audits = []
    monkeypatch.setattr(documents_router, "DocumentRepository", Repository)
    monkeypatch.setattr(documents_router, "document_storage", lambda: Storage())
    monkeypatch.setattr(documents_router, "audit_event", lambda *args, **kwargs: audits.append((args, kwargs)))
    app.dependency_overrides[get_authenticated_user] = user
    try:
        response = client.get("/documents/document-1/download", headers={"Authorization": "Bearer valid"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == content_type
    assert response.headers["content-length"] == str(len(content))
    assert f'filename="{filename}"' in response.headers["content-disposition"]
    assert f"filename*=UTF-8''{filename}" in response.headers["content-disposition"]
    assert len(audits) == 1
    assert audits[0][0][1].id == "admin-1"
    assert audits[0][0][4] == "document-1"
    assert audits[0][0][5]["document_id"] == "document-1"
    assert "downloaded_at" in audits[0][0][5]
    assert "token" not in str(audits[0]).lower()


@pytest.mark.parametrize("role", ["viewer", "officer", "knowledge_manager"])
def test_every_non_admin_role_is_forbidden(monkeypatch, role):
    app.dependency_overrides[get_authenticated_user] = lambda: user(role)
    try:
        response = client.get("/documents/known-id/download", headers={"Authorization": "Bearer valid"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


def test_download_requires_bearer_even_when_global_auth_bypass_is_enabled(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "true")
    response = client.get("/documents/known-id/download")
    assert response.status_code == 401


@pytest.mark.parametrize("message", ["Invalid JWT", "JWT has expired", "User is inactive"])
def test_invalid_expired_and_disabled_authentication_return_401(monkeypatch, message):
    monkeypatch.setattr(auth, "_user_from_token", lambda _token: (_ for _ in ()).throw(AuthError(message)))
    response = client.get("/documents/known-id/download", headers={"Authorization": "Bearer rejected"})
    assert response.status_code == 401


def test_unknown_document_and_missing_original_are_indistinguishable(monkeypatch):
    monkeypatch.setattr(documents_router, "DocumentRepository", Repository)
    monkeypatch.setattr(documents_router, "document_storage", lambda: Storage())
    app.dependency_overrides[get_authenticated_user] = user
    try:
        Repository.document = None
        unknown = client.get("/documents/unknown/download", headers={"Authorization": "Bearer valid"})
        Repository.document = {"id": "missing", "source": "missing.pdf", "kind": "pdf", "metadata": {}}
        Storage.download = None
        missing = client.get("/documents/missing/download", headers={"Authorization": "Bearer valid"})
    finally:
        app.dependency_overrides.clear()

    assert unknown.status_code == missing.status_code == 404
    assert unknown.json() == missing.json()
    assert "missing.pdf" not in missing.text


def test_local_storage_rejects_traversal_and_streams_from_confined_file(monkeypatch, tmp_path):
    documents_root = tmp_path / "documents"
    documents_root.mkdir()
    monkeypatch.setattr(storage_module, "DOCS_DIR", documents_root)
    outside = tmp_path / "secret.pdf"
    inside = documents_root / "safe.pdf"
    outside.write_bytes(b"secret")
    inside.write_bytes(b"safe")

    storage = LocalDocumentStorage()
    assert storage.open_download("../secret.pdf", "application/pdf") is None
    download = storage.open_download("safe.pdf", "application/pdf")
    assert download is not None
    assert b"".join(download.chunks) == b"safe"


def test_malicious_unicode_filename_cannot_inject_headers_and_keeps_extension():
    filename = documents_router.safe_download_filename(' वन नियम\r\nX-Evil: yes";.pdf')
    header = documents_router.content_disposition(filename)

    assert filename.endswith(".pdf")
    assert "\r" not in header and "\n" not in header
    assert 'filename="' in header
    assert "filename*=UTF-8''" in header


def test_new_and_legacy_citations_use_stable_document_id():
    context = {
        "document_id": "document-1",
        "source": "rules.pdf",
        "chunk_index": 2,
        "page_start": 3,
        "page_end": 3,
        "section_heading": "Rule 4",
        "score": 0.8,
        "text": "Original evidence",
    }
    new_source = source_payload([context])[0]

    class Documents:
        def document_ids_by_sources(self, sources):
            assert sources == {"rules.pdf"}
            return {"rules.pdf": "document-1"}

    legacy = [{"role": "assistant", "sources": [{key: value for key, value in new_source.items() if key != "document_id"}]}]
    enriched = enrich_legacy_citations(legacy, document_repository=Documents())

    assert new_source["document_id"] == "document-1"
    assert enriched[0]["sources"][0]["document_id"] == "document-1"
    assert "document_id" not in legacy[0]["sources"][0]


def test_openapi_exposes_document_id_and_download_operation():
    schema = app.openapi()
    assert "document_id" in schema["components"]["schemas"]["SourceResponse"]["required"]
    assert "/documents/{document_id}/download" in schema["paths"]


def test_document_id_downloads_current_original_for_replaced_document(monkeypatch):
    """Document IDs are stable; replacements intentionally serve the current original."""
    Repository.document = {"id": "stable-id", "source": "rules.pdf", "kind": "pdf", "metadata": {"revision": 2}}
    Storage.download = DocumentDownload("rules.pdf", "application/pdf", 9, iter([b"revision", b"2"]))
    monkeypatch.setattr(documents_router, "DocumentRepository", Repository)
    monkeypatch.setattr(documents_router, "document_storage", lambda: Storage())
    monkeypatch.setattr(documents_router, "audit_event", lambda *_args, **_kwargs: None)
    app.dependency_overrides[get_authenticated_user] = user
    try:
        response = client.get("/documents/stable-id/download", headers={"Authorization": "Bearer valid"})
    finally:
        app.dependency_overrides.clear()

    assert response.content == b"revision2"
