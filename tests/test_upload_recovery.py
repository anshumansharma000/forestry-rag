from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

import routers.documents as routes
import upload_policy
from app import app
from auth import CurrentUser, get_current_user
from consistency import OperationBusy
from errors import AppError
from repositories import DocumentRepository
from schemas import CompleteDirectUploadFileRequest, DirectUploadFileRequest


@pytest.fixture
def recovery(monkeypatch):
    states = {"failed.pdf": "failed", "indexed.pdf": "indexed", "busy.pdf": "processing", "orphan.pdf": "unindexed"}
    calls = []

    class Storage:
        prefix = "docs/"

        def exists(self, source):
            return source in states

        def save(self, source, content):
            calls.append(("save", source, content))
            return "/docs/" + source

        def key_for(self, source):
            return "docs/" + source

        def presigned_put_url(self, key, content_type, expires):
            calls.append(("presign", key))
            return "https://example.invalid/put"

        def head_key(self, key):
            return {"ContentLength": 12, "ContentType": "application/pdf"}

        def copy_key(self, source, target):
            calls.append(("copy", source, target))
            return "/docs/" + target

        def delete_key(self, key):
            calls.append(("delete", key))

    class Repository:
        def upload_state(self, source):
            calls.append(("state", source))
            return states.get(source, "unindexed")

        @contextmanager
        def index_lease(self):
            calls.append(("lock",))
            try:
                yield SimpleNamespace(check=lambda: calls.append(("check",)))
            finally:
                calls.append(("unlock",))

    monkeypatch.setattr(upload_policy, "DocumentRepository", Repository)
    monkeypatch.setattr(routes, "document_storage", Storage)
    monkeypatch.setattr(routes, "audit_event", lambda *a, **kw: calls.append(("audit", a[5])))
    monkeypatch.setenv("R2_UPLOAD_STAGING_PREFIX", "pending/")
    return SimpleNamespace(states=states, calls=calls, user=SimpleNamespace(id="manager"), repository=Repository)


def test_failed_duplicate_prompts_before_any_storage_write(recovery):
    with pytest.raises(AppError) as result:
        routes.save_uploads(None, recovery.user, [routes.PendingUpload("failed.pdf", b"new", "application/pdf")])
    assert result.value.status_code == 409
    assert result.value.details["can_replace_failed"] is True
    assert result.value.details["requires_confirmation"] is True
    assert result.value.details["conflicts"] == [{
        "index": 0, "filename": "failed.pdf", "state": "failed", "can_replace_failed": True, "requires_confirmation": True,
    }]
    assert not any(call[0] in {"save", "copy", "presign"} for call in recovery.calls)


@pytest.mark.parametrize("source", ["failed.pdf", "orphan.pdf"])
def test_confirmed_replacement_uses_ingestion_lock_and_audits(recovery, source):
    result = routes.save_uploads(None, recovery.user, [routes.PendingUpload(source, b"fixed", "application/pdf", replace_failed=True)])
    assert result[0]["filename"] == source
    events = [call[0] for call in recovery.calls]
    assert events.index("lock") < events.index("state") < events.index("check") < events.index("save") < events.index("unlock")
    assert next(call[1] for call in recovery.calls if call[0] == "audit")["replaced_failed"] is True


@pytest.mark.parametrize("source", ["indexed.pdf", "busy.pdf"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_indexed_and_processing_documents_stay_protected(recovery, monkeypatch, source, confirmed):
    monkeypatch.setenv("ALLOW_DOCUMENT_REPLACE", "true")
    with pytest.raises(AppError) as error:
        routes.save_uploads(None, recovery.user, [routes.PendingUpload(source, b"new", "application/pdf", replace_failed=confirmed)])
    assert error.value.status_code == 409
    assert error.value.details["can_replace_failed"] is False
    assert not any(call[0] in {"save", "copy"} for call in recovery.calls)


def test_mixed_batch_reports_every_duplicate_before_writing(recovery):
    with pytest.raises(AppError) as error:
        routes.save_uploads(None, recovery.user, [
            routes.PendingUpload("new.pdf", b"new", "application/pdf"),
            routes.PendingUpload("failed.pdf", b"fixed", "application/pdf"),
            routes.PendingUpload("indexed.pdf", b"other", "application/pdf"),
        ])
    assert [item["index"] for item in error.value.details["conflicts"]] == [1, 2]
    assert error.value.details["can_replace_failed"] is False
    assert not any(call[0] == "save" for call in recovery.calls)


def test_direct_upload_rechecks_state_when_finalizing(recovery):
    [upload] = routes.create_presigned_uploads(recovery.user, [routes.PendingDirectUpload(
        "failed.pdf", 12, "application/pdf", replace_failed=True,
    )])
    assert upload["replace_failed"] is True
    recovery.states["failed.pdf"] = "indexed"
    with pytest.raises(AppError) as error:
        routes.complete_direct_uploads(None, recovery.user, [routes.CompletedDirectUpload(
            upload["upload_id"], "failed.pdf", replace_failed=True,
        )])
    assert error.value.details["can_replace_failed"] is False
    assert not any(call[0] == "copy" for call in recovery.calls)
    assert any(call[0] == "delete" and call[1].startswith("pending/") for call in recovery.calls)


def test_confirmed_direct_upload_keeps_staging_isolated_and_can_complete(recovery):
    [upload] = routes.create_presigned_uploads(recovery.user, [routes.PendingDirectUpload(
        "failed.pdf", 12, "application/pdf", replace_failed=True,
    )])
    result = routes.complete_direct_uploads(None, recovery.user, [routes.CompletedDirectUpload(
        upload["upload_id"], "failed.pdf", replace_failed=True,
    )])
    assert result[0]["filename"] == "failed.pdf"
    copy = next(call for call in recovery.calls if call[0] == "copy")
    assert copy[1].startswith("pending/manager/")
    assert copy[2] == "docs/failed.pdf"


def test_direct_confirmation_must_also_be_supplied_on_completion(recovery):
    with pytest.raises(AppError) as error:
        routes.complete_direct_uploads(None, recovery.user, [routes.CompletedDirectUpload(uuid4(), "failed.pdf")])
    assert error.value.details["requires_confirmation"] is True
    assert not any(call[0] == "copy" for call in recovery.calls)


def test_busy_ingestion_lease_prevents_source_replacement(recovery, monkeypatch):
    class Busy(recovery.repository):
        @contextmanager
        def index_lease(self):
            raise OperationBusy()
            yield

    monkeypatch.setattr(upload_policy, "DocumentRepository", Busy)
    with pytest.raises(OperationBusy):
        routes.save_uploads(None, recovery.user, [routes.PendingUpload("failed.pdf", b"new", "application/pdf", replace_failed=True)])
    assert not any(call[0] == "save" for call in recovery.calls)


def test_optional_confirmation_flags_flow_through_all_request_adapters():
    file = UploadFile(BytesIO(b"pdf"), filename="failed.pdf")
    assert routes.prepare_uploads([file], replace_failed=True)[0].replace_failed is True
    request = DirectUploadFileRequest(filename="failed.pdf", size_bytes=12, replace_failed=True)
    assert routes.prepare_direct_uploads([request])[0].replace_failed is True
    complete = CompleteDirectUploadFileRequest(upload_id=uuid4(), filename="failed.pdf", replace_failed=True)
    assert routes.prepare_completed_direct_uploads([complete])[0].replace_failed is True
    assert DirectUploadFileRequest(filename="new.pdf", size_bytes=12).replace_failed is False


def test_warning_and_confirmed_retry_work_through_multipart_api(recovery):
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id="manager", email="test@example.com", role="knowledge_manager")
    try:
        client = TestClient(app)
        response = client.post("/documents/upload", files={"file": ("failed.pdf", b"new", "application/pdf")})
        assert response.status_code == 409
        assert response.json()["error"]["details"]["requires_confirmation"] is True
        response = client.post("/documents/upload", files={"file": ("failed.pdf", b"new", "application/pdf")},
                               data={"replace_failed": "true"})
        assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("metadata,chunks,jobs,expected", [
    ({"ingest_status": "indexed", "last_refresh_error": "failed later"}, False, False, "indexed"),
    ({"ingest_status": "failed"}, True, False, "indexed"),
    ({"ingest_status": "failed"}, False, True, "processing"),
    ({"ingest_status": "failed"}, False, False, "failed"),
    ({"ingest_status": "uploaded"}, False, False, "unindexed"),
    (None, False, False, "unindexed"),
])
def test_repository_prefers_published_evidence_and_active_jobs(metadata, chunks, jobs, expected):
    calls = []

    class Query:
        def __init__(self, table):
            self.name = table

        def __getattr__(self, method):
            def query(*args, **kwargs):
                calls.append((self.name, method, args))
                if method == "execute":
                    data = {"documents": [{"id": "document", "metadata": metadata}] if metadata is not None else [],
                            "document_chunks": [{"id": "chunk"}] if chunks else [], "ingest_jobs": [{"id": "job"}] if jobs else []}
                    return SimpleNamespace(data=data[self.name])
                return self
            return query

    repository = DocumentRepository(SimpleNamespace(table=Query))
    assert repository.upload_state("failed.pdf") == expected
    if expected in {"processing", "failed", "unindexed"}:
        assert ("ingest_jobs", "in_", ("status", ["queued", "running"])) in calls
        assert ("ingest_jobs", "or_", ('metadata->>source.eq."failed.pdf",metadata->>scope.eq.corpus',)) in calls
