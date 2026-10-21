import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.common.errors import BusinessException, ErrorCode
from app.common.response import ApiResponse
from app.main import create_app


class InquiryRequest(BaseModel):
    symptom: str
    photo_count: int


@pytest.fixture
def app() -> FastAPI:
    app = create_app()

    @app.get("/test/ok")
    def ok() -> ApiResponse[dict]:
        return ApiResponse.ok({"symptom": "온수 불량"})

    @app.get("/test/business")
    def business():
        raise BusinessException(ErrorCode.RESOURCE_NOT_FOUND)

    @app.post("/test/validate")
    def validate(body: InquiryRequest):
        return ApiResponse.ok()

    @app.get("/test/crash")
    def crash():
        raise RuntimeError("boom")

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    # 500 처리 결과를 확인하려면 서버 예외를 테스트로 다시 던지지 않아야 한다
    return TestClient(app, raise_server_exceptions=False)


def test_success_uses_backend_format(client: TestClient):
    body = client.get("/test/ok").json()

    assert body == {
        "success": True,
        "code": "OK",
        "message": "요청 성공",
        "result": {"symptom": "온수 불량"},
    }


def test_business_exception_uses_error_code(client: TestClient):
    response = client.get("/test/business")

    assert response.status_code == 404
    assert response.json() == {
        "success": False,
        "code": "RESOURCE_NOT_FOUND",
        "message": "요청한 리소스를 찾을 수 없습니다",
        "result": None,
    }


def test_validation_error_lists_fields(client: TestClient):
    response = client.post("/test/validate", json={"photo_count": "many"})

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_INPUT"
    assert {e["field"] for e in body["result"]} == {"symptom", "photo_count"}


def test_broken_json_is_invalid_input_without_fields(client: TestClient):
    response = client.post(
        "/test/validate", content="{broken", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
    assert response.json()["result"] is None


def test_unknown_url_is_not_found(client: TestClient):
    response = client.get("/nope")

    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


def test_wrong_method_is_not_allowed(client: TestClient):
    response = client.delete("/health")

    assert response.status_code == 405
    assert response.json()["code"] == "METHOD_NOT_ALLOWED"


def test_unexpected_error_hides_details(client: TestClient):
    response = client.get("/test/crash")

    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_SERVER_ERROR"
    assert "boom" not in response.text
