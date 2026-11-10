"""Bedrock 인증 위임·구조화 출력·API 오류 계약을 실제 AWS 호출 없이 검증한다."""

import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    NoCredentialsError,
    NoRegionError,
    ReadTimeoutError,
)
from fastapi.testclient import TestClient

from app.intake.bedrock_provider import TOOL_NAME, BedrockExtractionProvider
from app.intake.contracts import Message, Photo
from app.intake.providers import ExtractedValue, ModelOutput
from app.main import create_app
from tests.bedrock_fakes import bedrock_response, sdk_client, sdk_session
from tests.test_intake_graph import payload

DEMO_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


@pytest.mark.parametrize("model", [DEMO_MODEL, "future-compatible-model-id"])
def test_model_environment_and_default_aws_credentials_are_used(monkeypatch, model):
    monkeypatch.setenv("BEDROCK_MODEL_ID", model)
    monkeypatch.setenv("INTERNAL_KEY", "test-internal")
    client = sdk_client(ModelOutput())
    session = sdk_session(client)
    with patch("boto3.session.Session", return_value=session) as factory:
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=payload().model_dump(mode="json"),
            headers={"X-Internal-Key": "test-internal"},
        )
    assert response.status_code == 200
    assert response.json()["result"]["modelName"] == model
    factory.assert_called_once_with()
    assert session.client.call_args.args == ("bedrock-runtime",)
    assert set(session.client.call_args.kwargs) == {"config"}
    config = session.client.call_args.kwargs["config"]
    assert config.retries == {"mode": "standard", "total_max_attempts": 1}
    assert config.connect_timeout + config.read_timeout <= 25
    sent = client.converse.call_args.kwargs
    assert sent["modelId"] == model
    assert sent["additionalModelRequestFields"] == {"thinking": {"type": "disabled"}}
    assert sent["toolConfig"]["toolChoice"] == {"tool": {"name": TOOL_NAME}}
    assert "temperature" not in sent["inferenceConfig"]
    assert client.converse.call_count == 1
    client.close.assert_called_once()


@pytest.mark.parametrize("model", [None, "", "   "])
def test_missing_model_fails_without_discovering_aws_credentials(monkeypatch, model):
    monkeypatch.setenv("INTERNAL_KEY", "test-internal")
    if model is not None:
        monkeypatch.setenv("BEDROCK_MODEL_ID", model)
    with patch("boto3.session.Session") as factory:
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=payload().model_dump(mode="json"),
            headers={"X-Internal-Key": "test-internal"},
        )
    assert response.status_code == 502
    assert response.json()["code"] == "AI_MODEL_ERROR"
    factory.assert_not_called()


@pytest.mark.parametrize(
    "error,code,status",
    [
        (NoCredentialsError(), "AI_MODEL_ERROR", 502),
        (NoRegionError(), "AI_MODEL_ERROR", 502),
        (
            ClientError(
                {"Error": {"Code": "ModelTimeoutException", "Message": "private"}}, "Converse"
            ),
            "AI_MODEL_TIMEOUT",
            504,
        ),
        (
            ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "private"}}, "Converse"
            ),
            "AI_MODEL_ERROR",
            502,
        ),
        (
            ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "private"}}, "Converse"
            ),
            "AI_MODEL_ERROR",
            502,
        ),
        (ReadTimeoutError(endpoint_url="https://bedrock.invalid"), "AI_MODEL_TIMEOUT", 504),
        (ConnectTimeoutError(endpoint_url="https://bedrock.invalid"), "AI_MODEL_TIMEOUT", 504),
    ],
)
def test_aws_failures_follow_api_contract_without_retry(monkeypatch, caplog, error, code, status):
    monkeypatch.setenv("BEDROCK_MODEL_ID", DEMO_MODEL)
    monkeypatch.setenv("INTERNAL_KEY", "test-internal")
    client = sdk_client()
    client.converse.side_effect = error
    with patch("boto3.session.Session", return_value=sdk_session(client)):
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=payload().model_dump(mode="json"),
            headers={"X-Internal-Key": "test-internal"},
        )
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.json()["result"] is None
    assert client.converse.call_count == 1
    assert "private" not in caplog.text
    client.close.assert_called_once()


@pytest.mark.parametrize("async_mode", [False, True])
def test_bedrock_reuses_customer_evidence_filter(async_mode):
    output = ModelOutput(
        values=[
            ExtractedValue(
                fieldKey="symptom", value="온수 안됨", observedText="온수 안됨", messageId=1
            ),
            ExtractedValue(
                fieldKey="model", value="invented", observedText="없는 근거", messageId=1
            ),
        ]
    )
    client = sdk_client(output)
    provider = BedrockExtractionProvider(DEMO_MODEL)
    messages = [
        Message(messageId=1, sender="CUSTOMER", content="온수 안됨", createdAt="2026-11-09")
    ]
    with patch("boto3.session.Session", return_value=sdk_session(client)):
        result = (
            asyncio.run(provider.aextract_message(messages))
            if async_mode
            else provider.extract_message(messages)
        )
    assert [(entry.fieldKey, entry.evidence.messageId) for entry in result.observations] == [
        ("symptom", 1)
    ]
    assert provider.model_calls == provider.usage_reported_calls == 1
    assert (provider.input_tokens, provider.output_tokens) == (12, 8)


@pytest.mark.parametrize(
    "failure", ["truncated", "refused", "missing", "wrong_tool", "multiple", "invalid"]
)
def test_invalid_tool_output_fails_and_preserves_usage(failure):
    response = deepcopy(bedrock_response(ModelOutput()))
    blocks = response["output"]["message"]["content"]
    if failure == "truncated":
        response["stopReason"] = "max_tokens"
    elif failure == "refused":
        response["stopReason"] = "guardrail_intervened"
    elif failure == "missing":
        blocks[0]["toolUse"]["input"] = {}
    elif failure == "wrong_tool":
        blocks[0]["toolUse"]["name"] = "unexpected"
    elif failure == "multiple":
        blocks.append(deepcopy(blocks[0]))
    else:
        blocks[0]["toolUse"]["input"]["values"] = [{"fieldKey": "unknown", "value": "x"}]
    client = sdk_client()
    client.converse.return_value = response
    client.converse.side_effect = None
    provider = BedrockExtractionProvider(DEMO_MODEL)
    with (
        patch("boto3.session.Session", return_value=sdk_session(client)),
        pytest.raises(ValueError),
    ):
        provider._parse("extract", [{"type": "input_text", "text": "sample"}])
    assert provider.model_calls == provider.usage_reported_calls == 1
    assert (provider.input_tokens, provider.output_tokens) == (12, 8)
    assert client.converse.call_count == 1


def test_expired_budget_never_creates_client():
    provider = BedrockExtractionProvider(DEMO_MODEL)
    provider._deadline = 0
    with patch("boto3.session.Session") as factory, pytest.raises(TimeoutError):
        provider._parse("extract", [{"type": "input_text", "text": "sample"}])
    factory.assert_not_called()


def test_photo_does_not_leave_for_bedrock_or_count_as_analyzed(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", DEMO_MODEL)
    monkeypatch.setenv("INTERNAL_KEY", "test-internal")
    request = payload().model_copy(
        update={
            "photos": [
                Photo(photoId=7, messageId=1, imageUrl="https://s3.example.com/signed-photo")
            ]
        }
    )
    client = sdk_client(ModelOutput())
    with patch("boto3.session.Session", return_value=sdk_session(client)):
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=request.model_dump(mode="json"),
            headers={"X-Internal-Key": "test-internal"},
        )
    assert response.status_code == 200
    assert response.json()["result"]["analyzedPhotoIds"] == []
    assert client.converse.call_count == 1
    content = client.converse.call_args.kwargs["messages"][0]["content"]
    assert all(set(block) == {"text"} and "signed-photo" not in block["text"] for block in content)


def test_bedrock_rejects_image_input_before_contacting_aws():
    provider = BedrockExtractionProvider(DEMO_MODEL)
    with patch("boto3.session.Session") as factory, pytest.raises(ValueError, match="텍스트"):
        provider._parse("extract", [{"type": "input_image", "image_url": "https://example.com"}])
    factory.assert_not_called()


def test_real_sdk_accepts_converse_request_shape():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.stub import Stubber

    # 서명을 끄고 SDK 입출력 형식만 검증한다. 인증 탐색·외부 통신은 하지 않는다.
    client = boto3.session.Session().client(
        "bedrock-runtime", region_name="us-east-1", config=Config(signature_version=UNSIGNED)
    )
    provider = BedrockExtractionProvider(DEMO_MODEL)
    with (
        Stubber(client) as stub,
        patch("boto3.session.Session", return_value=sdk_session(client)),
    ):
        stub.add_response("converse", bedrock_response(ModelOutput()))
        assert provider._parse("extract", [{"type": "input_text", "text": "sample"}]).values == []
        stub.assert_no_pending_responses()
