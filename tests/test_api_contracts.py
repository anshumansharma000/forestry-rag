import pytest
from fastapi.testclient import TestClient

import routers.documents as documents_router
from app import app
from auth import CurrentUser, get_current_user

client = TestClient(app)


def test_health_response_is_standard_status_shape():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_validation_errors_use_standard_error_envelope():
    response = client.post("/auth/login", json={})

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["message"] == "Request validation failed"
    assert "errors" in payload["error"]["details"]


def test_auth_errors_use_standard_error_envelope():
    response = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "auth_error",
            "message": "Bearer token is required",
            "details": {},
        }
    }


@pytest.mark.parametrize("role", ["viewer", "officer"])
def test_document_library_rejects_roles_below_knowledge_manager(role):
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=f"{role}-1",
        email=f"{role}@example.com",
        role=role,
    )
    try:
        response = client.get("/documents")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Insufficient role permissions"


@pytest.mark.parametrize("role", ["knowledge_manager", "admin"])
def test_document_library_allows_knowledge_managers_and_admins(monkeypatch, role):
    expected = {
        "items": [],
        "pagination": {"offset": 0, "limit": 25, "total": 0, "has_more": False},
    }

    class Repository:
        def list_documents(self, **_kwargs):
            return expected

    monkeypatch.setattr(documents_router, "DocumentRepository", Repository)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=f"{role}-1",
        email=f"{role}@example.com",
        role=role,
    )
    try:
        response = client.get("/documents")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == expected
