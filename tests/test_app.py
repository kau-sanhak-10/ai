from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app


def test_health_returns_up(client: TestClient):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "UP"}


def test_docs_open_in_local(client: TestClient):
    assert client.get("/docs").status_code == 200


def test_docs_closed_in_prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")

    client = TestClient(create_app())

    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_empty_api_key_is_treated_as_missing(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")

    assert get_settings().gemini_api_key is None


def test_api_key_is_hidden_when_printed(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "real-secret")

    settings = get_settings()

    assert "real-secret" not in repr(settings)
    assert settings.gemini_api_key.get_secret_value() == "real-secret"
