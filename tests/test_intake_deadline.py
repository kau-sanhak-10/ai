"""서비스의 전체 시간 예산과 비동기 모델 호출 취소를 검증한다."""

import asyncio
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.intake.bedrock_provider import BedrockExtractionProvider
from app.intake.contracts import CategoryDecision, Evidence, RawObservation, Understanding
from app.intake.providers import ExtractedValue, ModelOutput
from app.main import create_app
from tests.bedrock_fakes import bedrock_response, sdk_client, sdk_session
from tests.test_intake_graph import FakeProvider, payload, symptom


class SlowAsyncProvider(FakeProvider):
    def __init__(self):
        super().__init__(text_facts=[symptom()])
        self.started_photos = []
        self.cancelled_photos = []

    async def aextract_message(self, messages):
        await asyncio.sleep(0.15)
        return super().extract_message(messages)

    async def aanalyze_image(self, photo):
        self.started_photos.append(photo.photoId)
        try:
            if photo.photoId == 57:
                await asyncio.Future()
            await asyncio.sleep(0.15)
            return super().analyze_image(photo)
        except asyncio.CancelledError:
            self.cancelled_photos.append(photo.photoId)
            raise

    def extract_message(self, messages):
        raise AssertionError("서비스는 취소 가능한 비동기 경로를 사용해야 합니다")

    def analyze_image(self, photo):
        raise AssertionError("서비스는 취소 가능한 비동기 경로를 사용해야 합니다")


def test_deadline_is_shared_by_text_and_all_photos(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "unit-test")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "unit-test-model")
    monkeypatch.setenv("INTAKE_TIMEOUT_SECONDS", "0.6")
    get_settings.cache_clear()
    provider = SlowAsyncProvider()
    body = payload(
        photos=[
            {"photoId": photo_id, "messageId": 900, "imageUrl": "https://example.org/photo.jpg"}
            for photo_id in (56, 57)
        ]
    ).model_dump(mode="json")
    with patch("app.api.inquiries.BedrockExtractionProvider", return_value=provider):
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=body,
            headers={"X-Internal-Key": "unit-test"},
        )
    assert response.status_code == 504
    assert response.json()["code"] == "AI_MODEL_TIMEOUT"
    assert response.json()["result"] is None
    assert provider.started_photos == [56, 57]
    assert provider.cancelled_photos == [57]


def test_sdk_timeout_stops_waiting_and_closes_client_after_worker_finishes(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "unit-test")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "unit-test-model")
    monkeypatch.setenv("INTAKE_TIMEOUT_SECONDS", "0.1")
    get_settings.cache_clear()
    release = threading.Event()
    closed = threading.Event()
    client = sdk_client()

    def slow_converse(**kwargs):
        release.wait(3)
        return bedrock_response(ModelOutput())

    client.converse.side_effect = slow_converse
    client.close.side_effect = lambda *_: closed.set()
    started = time.monotonic()
    try:
        with patch("boto3.session.Session", return_value=sdk_session(client)):
            response = TestClient(create_app()).post(
                "/internal/v1/inquiries/analyze",
                json=payload().model_dump(mode="json"),
                headers={"X-Internal-Key": "unit-test"},
            )
        assert response.status_code == 504
        assert response.json()["code"] == "AI_MODEL_TIMEOUT"
        assert time.monotonic() - started < 1
        assert client.converse.call_count == 1
        assert not closed.is_set()
    finally:
        release.set()
        assert closed.wait(2)
    client.close.assert_called_once()


def test_async_sdk_success_parses_output_and_usage():
    client = sdk_client(ModelOutput())
    provider = BedrockExtractionProvider("unit-test-model")
    session = sdk_session(client)
    with patch("boto3.session.Session", return_value=session) as factory:
        result = asyncio.run(provider._aparse("extract", [{"type": "input_text", "text": "hi"}]))
    assert result.values == []
    assert (provider.input_tokens, provider.output_tokens) == (12, 8)
    factory.assert_called_once_with()
    assert session.client.call_args.kwargs["config"].retries["total_max_attempts"] == 1
    client.close.assert_called_once()


def test_async_service_analyzes_only_new_photo_and_returns_grounded_facts(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "unit-test")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "unit-test-model")
    get_settings.cache_clear()
    client = sdk_client(
        ModelOutput(
            values=[
                ExtractedValue(
                    fieldKey="symptom", value="온수가 안 나와요", observedText="온수가 안 나와요"
                )
            ],
            intent="ANSWER",
            workCategory=CategoryDecision(value="REPAIR", status="CONFIRMED"),
        ),
    )
    req = payload(
        photos=[
            {"photoId": photo_id, "messageId": 900, "imageUrl": "https://example.org/photo.jpg"}
            for photo_id in (56, 57)
        ]
    )
    req.messages[0].content = "온수가 안 나와요."
    req.processedPhotoIds = [56]
    with (
        patch("boto3.session.Session", return_value=sdk_session(client)),
        patch(
            "app.intake.bedrock_provider.BedrockExtractionProvider.aanalyze_image",
            return_value=Understanding(
                observations=[
                    RawObservation(
                        fieldKey="model",
                        value="A-100",
                        source="PHOTO",
                        evidence=Evidence(photoId=57),
                        observedText="A-100",
                    )
                ]
            ),
        ) as analyze_photo,
    ):
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=req.model_dump(mode="json"),
            headers={"X-Internal-Key": "unit-test"},
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["analyzedPhotoIds"] == [57]
    assert result["nextAction"]["action"] == "CHECK_SCHEDULE"
    photo_fact = next(f for f in result["facts"] if f["fieldKey"] == "model")
    assert photo_fact["value"] == "A-100"
    assert photo_fact["evidence"]["photoId"] == 57
    assert analyze_photo.await_args.args[0].photoId == 57
    assert client.converse.call_count == 1
    assert client.close.call_count == 1


def test_text_and_new_photo_start_before_either_finishes(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "unit-test")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "unit-test-model")
    monkeypatch.setenv("INTAKE_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()

    class ConcurrentProvider(FakeProvider):
        text_started = asyncio.Event()
        photo_started = asyncio.Event()

        async def aextract_message(self, messages):
            self.text_started.set()
            await self.photo_started.wait()
            return Understanding(observations=[symptom()])

        async def aanalyze_image(self, photo):
            self.photo_started.set()
            await self.text_started.wait()
            return Understanding()

    req = payload(photos=[{"photoId": 56, "messageId": 900, "imageUrl": "https://example.org/x"}])
    with patch("app.api.inquiries.BedrockExtractionProvider", return_value=ConcurrentProvider()):
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=req.model_dump(mode="json"),
            headers={"X-Internal-Key": "unit-test"},
        )
    assert response.status_code == 200
    assert response.json()["result"]["analyzedPhotoIds"] == [56]


@pytest.mark.parametrize("seconds", [0, -1, 30, 31])
def test_deadline_must_be_positive_and_less_than_spring_timeout(seconds):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, intake_timeout_seconds=seconds)


@pytest.mark.parametrize(
    "failure, status, code",
    [
        ("timeout", 504, "AI_MODEL_TIMEOUT"),
        ("api", 502, "AI_MODEL_ERROR"),
        ("invalid", 502, "AI_OUTPUT_INVALID"),
    ],
)
def test_partial_photo_success_is_never_published_as_a_success(monkeypatch, failure, status, code):
    monkeypatch.setenv("INTERNAL_KEY", "unit-test")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "unit-test-model")
    monkeypatch.setenv("INTAKE_TIMEOUT_SECONDS", "0.6" if failure == "timeout" else "2")
    get_settings.cache_clear()

    class PartialPhotoProvider(FakeProvider):
        def __init__(self):
            super().__init__(text_facts=[symptom()])
            self.started = []
            self.finished = []
            self.active = set()
            self.cancelled = []
            self.text_cancelled = False

        async def aextract_message(self, messages):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.text_cancelled = True
                raise

        async def aanalyze_image(self, photo):
            self.started.append(photo.photoId)
            self.active.add(photo.photoId)
            try:
                if photo.photoId == 56:
                    self.finished.append(56)
                    return Understanding()
                if failure == "timeout":
                    await asyncio.Future()
                if failure == "api":
                    raise RuntimeError("simulated provider failure")
                raise ValueError("simulated invalid output")
            except asyncio.CancelledError:
                self.cancelled.append(photo.photoId)
                raise
            finally:
                self.active.remove(photo.photoId)

    provider = PartialPhotoProvider()
    req = payload(
        photos=[
            {"photoId": photo_id, "messageId": 900, "imageUrl": "https://example.org/photo.jpg"}
            for photo_id in (56, 57, 58)
        ]
    )
    with patch("app.api.inquiries.BedrockExtractionProvider", return_value=provider):
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=req.model_dump(mode="json"),
            headers={"X-Internal-Key": "unit-test"},
        )
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.json()["result"] is None
    assert provider.started == [56, 57]
    assert provider.finished == [56]
    assert provider.active == set()
    assert provider.text_cancelled
    assert provider.cancelled == ([57] if failure == "timeout" else [])
    assert req.processedPhotoIds == []


def test_photo_format_failure_does_not_retry_or_mark_complete(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "unit-test")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "unit-test-model")
    get_settings.cache_clear()
    text = ModelOutput(
        values=[
            ExtractedValue(
                fieldKey="symptom",
                value="온수가 안 나와요",
                observedText="온수가 안 나와요",
            )
        ],
        workCategory=CategoryDecision(value="REPAIR", status="CONFIRMED"),
    )
    client = sdk_client(text)
    req = payload(
        photos=[
            {
                "photoId": 56,
                "messageId": 900,
                "imageUrl": "https://example.org/photo.jpg",
            }
        ]
    )
    req.messages[0].content = "온수가 안 나와요."
    with (
        patch("boto3.session.Session", return_value=sdk_session(client)),
        patch(
            "app.intake.bedrock_provider.BedrockExtractionProvider.aanalyze_image",
            side_effect=ValueError("invalid photo pipeline output"),
        ) as analyze_photo,
    ):
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=req.model_dump(mode="json"),
            headers={"X-Internal-Key": "unit-test"},
        )
    analyze_photo.assert_awaited_once()
    assert response.status_code == 502
    assert response.json()["code"] == "AI_OUTPUT_INVALID"
    assert response.json()["result"] is None
