"""API 비용 없이 문자 근거·사진 입력·형식 오류 재시도를 검증한다."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.intake.contracts import Message, Photo
from app.intake.providers import (
    ExtractedValue,
    ModelOutput,
    OpenAIExtractionProvider,
)


def test_text_provenance_and_previous_messages():
    provider = OpenAIExtractionProvider("unit-test-only", "unit-test-model")
    provider._parse = lambda *_: ModelOutput(
        values=[
            ExtractedValue(fieldKey="symptom", value="난방 안됨", observedText="난방 안됨"),
            ExtractedValue(fieldKey="error_code", value="03", observedText="03"),
            ExtractedValue(fieldKey="model", value="invented", observedText="없는 문자열"),
        ],
        intent="ANSWER",
    )
    result = provider.extract_message(
        [
            Message(messageId=1, sender="CUSTOMER", content="난방 안됨", createdAt="2026-10-20"),
            Message(messageId=2, sender="CUSTOMER", content="03", createdAt="2026-10-21"),
        ]
    )
    assert [(x.fieldKey, x.evidence.messageId) for x in result.observations] == [
        ("symptom", 1),
        ("error_code", 2),
    ]


def test_photo_observation_uses_photo_id_only():
    provider = OpenAIExtractionProvider("unit-test-only", "unit-test-model")
    provider._parse = lambda *_: ModelOutput(
        values=[
            ExtractedValue(fieldKey="manufacturer", value="경동나비엔", observedText="경동나비엔"),
            ExtractedValue(fieldKey="model", value=None, unreadable=True),
        ]
    )
    result = provider.analyze_image(
        Photo(photoId=56, messageId=900, imageUrl="https://example.com/photo.jpg")
    )
    assert result.observations[0].evidence.photoId == 56
    assert result.observations[1].unconfirmedReason == "PHOTO_UNREADABLE"
    assert result.observations[1].value is None


def test_structured_output_none_retries_once():
    import sys

    client = MagicMock()
    client.__enter__.return_value = client
    client.responses.parse.side_effect = [
        SimpleNamespace(output_parsed=None),
        SimpleNamespace(
            output_parsed=ModelOutput(values=[]),
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        ),
    ]
    provider = OpenAIExtractionProvider("unit-test-only", "unit-test-model")
    with patch.dict(
        sys.modules, {"openai": SimpleNamespace(OpenAI=MagicMock(return_value=client))}
    ):
        result = provider._parse("only example", [{"type": "input_text", "text": "sample"}])
    assert result.values == []
    assert client.responses.parse.call_count == 2
    assert provider.input_tokens == 10
    assert provider.output_tokens == 5
