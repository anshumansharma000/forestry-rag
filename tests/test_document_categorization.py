import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import ingest_service
import routers.documents as routes
from app import app
from auth import CurrentUser, get_current_user
from document_categorization import CATEGORY_LABELS, CategorizationInput, infer_categorization, merge_categorization
from documents import extract_document_metadata
from repositories import DocumentRepository, document_library_item
from schemas import CompleteDirectUploadFileRequest


def test_full_document_dates_with_support_and_conflicts():
    pages = [{"page": n, "text": "Ordinary document text."} for n in range(1, 7)]
    pages[-1]["text"] = "Dated: 12 June 2023\nEffective from 1 July 2023\nRefers to the 1980 Act."
    metadata = extract_document_metadata("rules.pdf", "FCA Rules", pages)
    category = metadata["categorization"]
    assert category["values"]["issued_date"] == "2023-06-12"
    assert category["values"]["effective_date"] == "2023-07-01"
    assert category["evidence"]["issued_date"][0]["page"] == 6
    assert category["values"]["document_type"] == "rules"
    assert category["values"]["subjects"] == ["Forest conservation / FCA"]
    assert "issued_date" not in metadata  # Legacy extractor behavior remains stable.
    pages.append({"page": 7, "text": "Dated: 2 January 2024"})
    assert "issued_date" not in infer_categorization("rules.pdf", "Rules", pages)["values"]


def test_consolidation_cutoff_is_distinct_from_issue_date():
    result = infer_categorization("guide.pdf", "Consolidated Guidelines", [
        {"page": 1, "text": "Consolidated as of 1 July 2023\nDated: 2 August 2023\nState: Kerala"},
    ])
    assert result["values"]["document_type"] == "consolidated_guidelines"
    assert result["values"]["consolidation_as_of"] == "2023-07-01"
    assert result["values"]["issued_date"] == "2023-08-02"
    assert result["values"]["state"] == "Kerala"
    assert result["review_required"] is True


def test_effective_and_other_named_dates_are_not_issue_dates():
    result = infer_categorization("rules.pdf", "Rules", [{
        "page": 1, "text": "Effective date: 1 July 2023\nPublication date: 2 June 2023",
    }])
    assert result["values"]["effective_date"] == "2023-07-01"
    assert "issued_date" not in result["values"]
    assert result["evidence"]["effective_date"][0]["text"] == "Effective date: 1 July 2023"


def test_places_and_years_are_not_invented_metadata():
    result = infer_categorization("scan.pdf", "Scan", [{"page": 1, "text": "In Kerala during 1980, refer to Rule 2."}])
    assert "state" not in result["values"]
    assert "jurisdiction" not in result["values"]
    assert "issued_date" not in result["values"]
    assert "document_type" not in result["values"]
    assert "relationships" not in result["values"]


def test_guidelines_and_handbooks_can_reference_acts_and_rules_in_their_titles():
    for title, kind in (("Guidelines under the Forest Conservation Act", "guidelines"),
                        ("Handbook on Forest Conservation Rules", "handbook")):
        assert infer_categorization("file.pdf", title, [])["values"]["document_type"] == kind


def test_corrections_win_and_explicit_null_removes_a_suggestion():
    inferred = {"values": {"document_type": "act", "issued_date": "1980-01-01"}, "evidence": {"issued_date": []}}
    result = merge_categorization(inferred, {"overrides": {"document_type": "circular", "issued_date": None}})
    assert result["values"] == {"document_type": "circular", "issued_date": None}
    assert result["field_sources"] == {"document_type": "manual", "issued_date": "manual"}
    assert "issued_date" in result["missing_fields"]
    assert result["review_required"] is False


@pytest.mark.parametrize("body", [
    {"document_type": "made_up"}, {"reviewed": True}, {"issued_date": "2023-02-31"},
    {"registry_namespace": "production"}, {"subjects": [""]},
])
def test_invalid_or_server_owned_fields_rejected(body):
    with pytest.raises(ValidationError):
        CategorizationInput.model_validate(body)


@pytest.fixture
def api(monkeypatch):
    calls = []
    categorization = {"version": 1, "values": {"document_type": "rules"}, "overrides": {"document_type": "rules"}}

    class Storage:
        def exists(self, filename):
            return False

        def save(self, filename, content):
            calls.append(("save", filename, content))
            return f"/docs/{filename}"

    class Repository:
        def check_categorization_support(self):
            pass

        def save_categorization(self, source, patch, user):
            calls.append(("categorize", source, patch, user))
            return categorization

        def get_document(self, identifier):
            return {"id": identifier, "source": "rules.txt", "metadata": {"categorization": categorization}}

    monkeypatch.setattr(routes, "document_storage", Storage)
    monkeypatch.setattr(routes, "DocumentRepository", Repository)
    monkeypatch.setattr(routes, "audit_event", lambda *a: None)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id="manager", email="test@example.com", role="knowledge_manager")
    with TestClient(app) as client:
        yield client, calls
    app.dependency_overrides.clear()


def test_existing_upload_payload_and_response_still_work(api):
    client, calls = api
    result = client.post("/documents/upload", files={"file": ("rules.txt", b"Rules", "text/plain")})
    assert result.status_code == 200
    assert result.json() == {"status": "ok", "filename": "rules.txt", "path": "/docs/rules.txt"}
    assert [c[0] for c in calls] == ["save"]


def test_single_and_batch_uploads_store_optional_metadata(api):
    client, calls = api
    result = client.post("/documents/upload", files={"file": ("rules.txt", b"Rules")},
                         data={"categorization": json.dumps({"document_type": "rules", "issued_date": "2023-01-01"})})
    assert result.status_code == 200
    assert result.json()["categorization"]["values"]["document_type"] == "rules"
    assert calls[-1] == ("categorize", "rules.txt", {"document_type": "rules", "issued_date": "2023-01-01"}, "manager")
    result = client.post("/documents/uploads", files=[("files", ("one.txt", b"1")), ("files", ("two.txt", b"2"))],
                         data={"categorizations": json.dumps({"two.txt": {"document_type": "act"}})})
    assert result.status_code == 200
    assert "categorization" not in result.json()["files"][0]
    assert calls[-1] == ("categorize", "two.txt", {"document_type": "act"}, "manager")


@pytest.mark.parametrize("field,payload", [
    ("categorization", '{"document_type":"invalid"}'), ("categorization", "{"),
    ("categorization", '{"reviewed":true}'), ("categorizations", '{"wrong.txt":{"document_type":"act"}}'),
])
def test_invalid_metadata_rejected_before_file_is_saved(api, field, payload):
    client, calls = api
    single = field == "categorization"
    response = client.post("/documents/upload" if single else "/documents/uploads",
                           files={"file" if single else "files": ("rules.txt", b"Rules")}, data={field: payload})
    assert response.status_code == 422
    assert calls == []


def test_catalog_and_edit_existing_document_without_ingestion(api):
    client, calls = api
    catalog = client.get("/documents/categories")
    assert catalog.status_code == 200
    assert len(catalog.json()["categories"]) == len(CATEGORY_LABELS) == 13
    identifier = str(uuid4())
    assert client.get(f"/documents/{identifier}/categorization").status_code == 200
    response = client.patch(f"/documents/{identifier}/categorization", json={"document_type": "rules", "issued_date": None})
    assert response.status_code == 200
    assert calls == [("categorize", "rules.txt", {"document_type": "rules", "issued_date": None}, "manager")]


def test_completion_carries_per_file_categories_and_legacy_files_remain_valid():
    identifier = uuid4()
    assert CompleteDirectUploadFileRequest(upload_id=identifier, filename="rules.pdf").categorization is None
    request = CompleteDirectUploadFileRequest(upload_id=identifier, filename="rules.pdf", categorization={"document_type": "rules"})
    completed = routes.prepare_completed_direct_uploads([request])
    assert completed[0].categorization.document_type == "rules"


def test_direct_upload_completion_persists_metadata(monkeypatch):
    stored = []
    storage = SimpleNamespace(prefix="docs/", presigned_put_url=lambda *a: None,
        head_key=lambda key: {"ContentLength": 10}, key_for=lambda name: "docs/" + name,
        copy_key=lambda *a: "/docs/rules.pdf", delete_key=lambda key: None, exists=lambda name: False)
    monkeypatch.setattr(routes, "document_storage", lambda: storage)
    monkeypatch.setattr(routes, "audit_event", lambda *a: None)
    monkeypatch.setattr(routes, "DocumentRepository", lambda: SimpleNamespace(check_categorization_support=lambda: None))
    monkeypatch.setattr(routes, "persist_upload_categorization", lambda *a: stored.append(a) or {"values": {"document_type": "rules"}})
    metadata = CategorizationInput(document_type="rules")
    result = routes.complete_direct_uploads(
        None, SimpleNamespace(id="manager"), [routes.CompletedDirectUpload(uuid4(), "rules.pdf", metadata)],
    )
    assert stored == [("rules.pdf", metadata, "manager")]
    assert result[0]["categorization"]["values"]["document_type"] == "rules"


def test_ingestion_preserves_manual_fields_and_passes_a_snapshot_for_race_checks(monkeypatch):
    snapshots = []
    existing = {"overrides": {"document_type": "circular", "issued_date": None}, "registry_namespace": "old"}
    document = {"source": "rules.txt", "metadata": {"categorization": {"values": {"document_type": "rules"}}}}

    @contextmanager
    def lease():
        yield SimpleNamespace(token="lease", check=lambda: None)

    repository = SimpleNamespace(index_lease=lease, categorization_for_source=lambda source: existing,
        begin_revision=lambda doc: snapshots.append(doc) or {"already_indexed": True})
    monkeypatch.setenv("LEGAL_REGISTRY_WRITES_ENABLED", "false")
    monkeypatch.setattr(ingest_service, "iter_documents", lambda **kw: iter([document]))
    assert ingest_service.build_index(repository)["documents_skipped"] == 1
    snapshot = snapshots[0]
    assert snapshot["metadata"]["categorization"]["values"]["document_type"] == "circular"
    assert snapshot["metadata"]["issued_date"] is None
    assert snapshot["_expected_categorization"] == existing
    assert "registry_namespace" not in snapshot["metadata"]["categorization"]


def test_registry_gate_and_legacy_library_shape(monkeypatch):
    calls = []

    class Client:
        def rpc(self, name, params):
            calls.append((name, params))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data={}))

    repository = DocumentRepository(Client())
    monkeypatch.setenv("LEGAL_REGISTRY_WRITES_ENABLED", "false")
    repository.save_categorization("rules.txt", {"document_type": "rules"}, "manager")
    assert calls[-1][1]["p_namespace"] is None
    monkeypatch.setenv("LEGAL_REGISTRY_WRITES_ENABLED", "true")
    monkeypatch.setenv("LEGAL_REGISTRY_NAMESPACE", "dev")
    repository.save_categorization("rules.txt", {}, "manager")
    assert calls[-1][1]["p_namespace"] == "dev"
    row = {"id": "old", "source": "old.pdf", "metadata": {}}
    assert "categorization" not in document_library_item(row)


def test_category_support_checked_before_saving_any_files(api, monkeypatch):
    from errors import AppError, ErrorCode

    client, calls = api

    def unavailable():
        raise AppError("Apply categorization migration.", code=ErrorCode.CONFIG_ERROR)

    monkeypatch.setattr(routes, "DocumentRepository", lambda: SimpleNamespace(check_categorization_support=unavailable))
    result = client.post("/documents/upload", files={"file": ("rules.txt", b"Rules")},
                         data={"categorization": '{"document_type":"rules"}'})
    assert result.status_code >= 400
    assert calls == []


def test_category_routes_require_document_management_permissions(api):
    client, _ = api
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id="viewer", email="viewer@example.com", role="viewer")
    identifier = str(uuid4())
    assert client.get("/documents/categories").status_code == 403
    assert client.get(f"/documents/{identifier}/categorization").status_code == 403
    assert client.patch(f"/documents/{identifier}/categorization", json={"document_type": "rules"}).status_code == 403


def test_new_category_filter_is_independent_of_legacy_document_type():
    calls = []

    class Query:
        data = []
        count = 0

        def __getattr__(self, name):
            def method(*args, **kwargs):
                calls.append((name, args))
                return self
            return method

    DocumentRepository(Query()).list_documents(category="consolidated_guidelines", document_type="guidelines")
    assert ("eq", ("metadata->categorization->values->>document_type", "consolidated_guidelines")) in calls
    assert ("eq", ("metadata->>document_type", "guidelines")) in calls


def test_old_database_fallback_only_for_missing_additive_rpc():
    calls = []

    class MissingRPC(Exception):
        code = "PGRST202"

    class Client:
        def rpc(self, name, params):
            calls.append(name)
            if name == "begin_categorized_revision":
                raise MissingRPC()
            return SimpleNamespace(execute=lambda: SimpleNamespace(data={"id": "revision"}))

    repository = DocumentRepository(Client())
    document = {"source": "rules.txt", "_lease_token": "token", "_expected_categorization": {}}
    assert repository.begin_revision(document) == {"id": "revision"}
    assert calls == ["begin_categorized_revision", "begin_consistent_revision"]
    # Inferred-only metadata may have been published by an earlier ingestion
    # while the additive migration is pending; subsequent ingestion still works.
    document["_expected_categorization"] = {"values": {"document_type": "rules"}, "overrides": {}}
    assert repository.begin_revision(document) == {"id": "revision"}
    document["_expected_categorization"] = {"overrides": {"document_type": "rules"}}
    with pytest.raises(MissingRPC):
        repository.begin_revision(document)
