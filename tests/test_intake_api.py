"""Spring↔AI 내부 계약, 입력 검증·인증·출력 에러 검증."""

from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import get_settings
from app.intake.contracts import AnalyzeResult
from app.intake.providers import validate_photo_url
from app.intake.rules import load_rules
from app.main import create_app
from tests.test_intake_graph import FakeProvider, payload, run, symptom


def test_internal_key_required(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "only-backend-knows")
    get_settings.cache_clear()
    client = TestClient(create_app())
    body = payload().model_dump(mode="json")
    assert client.post("/internal/v1/inquiries/analyze", json=body).status_code == 401
    assert (
        client.post(
            "/internal/v1/inquiries/analyze", json=body, headers={"X-Internal-Key": "wrong"}
        ).status_code
        == 401
    )


def test_internal_contract_round_trip_with_mock(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "only-backend-knows")
    get_settings.cache_clear()
    client = TestClient(create_app())
    mocked_result = run(payload(), FakeProvider(text_facts=[symptom()]))
    with patch("app.api.inquiries._analyze", return_value=mocked_result):
        res = client.post(
            "/internal/v1/inquiries/analyze",
            headers={"X-Internal-Key": "only-backend-knows"},
            json=payload().model_dump(mode="json"),
        )
    assert res.status_code == 200
    assert res.json()["success"] is True
    parsed = AnalyzeResult.model_validate(res.json()["result"])
    assert parsed.nextAction.action == "CHECK_SCHEDULE"
    assert parsed.requestId == "r-1"


def test_invalid_input_rejected_as_field_errors(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "only-backend-knows")
    get_settings.cache_clear()
    body = payload().model_dump(mode="json")
    body["inputVersion"] = -1
    res = TestClient(create_app()).post(
        "/internal/v1/inquiries/analyze",
        json=body,
        headers={"X-Internal-Key": "only-backend-knows"},
    )
    assert res.status_code == 400
    assert res.json()["code"] == "INVALID_INPUT"


def test_foreign_photo_evidence_rejected_in_validate_result():
    provider = FakeProvider(
        text_facts=[symptom()],
        image_facts=[
            {
                "fieldKey": "manufacturer",
                "value": "귀뚜라미",
                "source": "PHOTO",
                "evidence": {"photoId": 999},
            }
        ],
    )
    try:
        run(
            payload(
                photos=[
                    {
                        "photoId": 56,
                        "messageId": 900,
                        "imageUrl": "https://example.org/temporary.png",
                    }
                ]
            ),
            provider,
        )
        raise AssertionError("허용되지 않은 사진 근거는 거부되어야 함")
    except ValueError as exc:
        assert "다른 문의의 사진" in str(exc)


def test_private_photo_url_rejected():
    for url in ["http://localhost/p.png", "https://127.0.0.1/a", "https://10.0.0.1/a"]:
        try:
            validate_photo_url(url)
            raise AssertionError(url)
        except ValueError:
            pass
    validate_photo_url("https://s3.ap-northeast-2.amazonaws.com/path?signature=abc")


def test_rules_file_is_explicit_and_fail_closed(tmp_path):
    rule = load_rules()
    assert rule.max_asks == 3
    try:
        load_rules(tmp_path / "missing.yaml")
        raise AssertionError()
    except RuntimeError as exc:
        assert str(exc) == "AI_RULES_UNAVAILABLE"


def test_cannot_spoof_owner_source_in_ai_facts():
    data = run(payload(), FakeProvider(text_facts=[symptom()])).model_dump()
    data["facts"][0]["source"] = "OWNER"
    try:
        AnalyzeResult.model_validate(data)
        raise AssertionError()
    except ValidationError:
        pass


def test_model_without_credentials_never_returns_fake_success(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "only-backend-knows")
    get_settings.cache_clear()
    res = TestClient(create_app()).post(
        "/internal/v1/inquiries/analyze",
        json=payload().model_dump(mode="json"),
        headers={"X-Internal-Key": "only-backend-knows"},
    )
    assert res.status_code == 502
    assert res.json()["code"] == "AI_MODEL_ERROR"
    assert res.json()["success"] is False


def test_owner_handler_mode_never_runs_ai(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "only-backend-knows")
    get_settings.cache_clear()
    data = payload().model_dump(mode="json")
    data["handlerMode"] = "OWNER"
    res = TestClient(create_app()).post(
        "/internal/v1/inquiries/analyze",
        json=data,
        headers={"X-Internal-Key": "only-backend-knows"},
    )
    assert res.status_code == 400
    assert res.json()["success"] is False


def test_schema_validates_evidence_requires_real_source():
    from app.intake.contracts import Fact

    try:
        Fact(
            fieldKey="manufacturer",
            value="경동나비엔",
            source="PHOTO",
            status="CONFIRMED",
            evidence={"messageId": 900},
        )
        raise AssertionError()
    except ValidationError:
        pass
