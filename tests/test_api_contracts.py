from fastapi.testclient import TestClient

from app import app

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
