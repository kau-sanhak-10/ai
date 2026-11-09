"""실제 Gemini SDK의 요청 직렬화를 검증하며 외부 API는 호출하지 않는다."""

import json
from unittest.mock import patch

import httpx
import pytest
from google import genai

from app.intake.contracts import AnalyzeRequest, CategoryDecision, Message
from app.intake.graph import build_workflow
from app.intake.providers import ExtractedValue, ModelOutput
from app.intake.rules import load_rules
from try_my_intake_gemini import GeminiGraphTestProvider


def test_nested_contract_schema_is_sent_as_json_schema():
    output = ModelOutput(
        values=[
            ExtractedValue(
                fieldKey="symptom", value="온수가 안 나와요", observedText="온수가 안 나와요"
            )
        ],
        intent="ANSWER",
    )
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": output.model_dump_json()}]}}
                ],
                "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 8},
            },
        )

    client_factory = genai.Client

    def make_client(*, api_key, http_options):
        http_options.httpx_client = httpx.Client(transport=httpx.MockTransport(respond))
        return client_factory(api_key=api_key, http_options=http_options, vertexai=False)

    provider = GeminiGraphTestProvider("unit-test-only", "unit-test-model")
    with patch("google.genai.Client", side_effect=make_client):
        result = provider.extract_message(
            [
                Message(
                    messageId=900,
                    sender="CUSTOMER",
                    content="온수가 안 나와요",
                    createdAt="2026-10-30",
                )
            ]
        )

    assert len(requests) == 1
    config = requests[0]["generationConfig"]
    assert "responseSchema" not in config
    assert config["responseJsonSchema"] == ModelOutput.model_json_schema()
    definitions = config["responseJsonSchema"]["$defs"]
    assert definitions["CategoryDecision"]["additionalProperties"] is False
    assert definitions["Safety"]["additionalProperties"] is False
    assert config["responseMimeType"] == "application/json"
    assert result.observations[0].value == "온수가 안 나와요"
    assert result.observations[0].evidence.messageId == 900
    assert (provider.input_tokens, provider.output_tokens) == (12, 8)


@pytest.mark.parametrize("debug", [False, True])
def test_extraction_reports_rejections_only_when_debug_enabled(capsys, debug):
    provider = GeminiGraphTestProvider("unit-test-only", "unit-test-model", debug_extraction=debug)
    output = ModelOutput(
        values=[
            ExtractedValue(fieldKey="manufacturer", value="경동나비엔", observedText="경동나비엔"),
            ExtractedValue(fieldKey="symptom", value="온수 불량"),
            ExtractedValue(fieldKey="symptom", value="온수 불량", observedText="온수 불량"),
            ExtractedValue(fieldKey="model", value=None),
        ]
    )
    with patch.object(provider, "_generate", return_value=output):
        result = provider.extract_message(
            [
                Message(
                    messageId=900,
                    sender="CUSTOMER",
                    content="경동나비엔 보일러인데 온수가 안 나와요.",
                    createdAt="2026-11-02",
                )
            ]
        )

    assert [(item.fieldKey, item.value) for item in result.observations] == [
        ("manufacturer", "경동나비엔")
    ]
    stderr = capsys.readouterr().err
    if debug:
        assert "missing_observed_text" in stderr
        assert "observed_text_not_in_message" in stderr
        assert "null_value" in stderr
        assert '"candidates": 4, "observations": 1' in stderr
        assert "unit-test-only" not in stderr
    else:
        assert stderr == ""


def test_raw_response_and_parsed_output_are_visible_in_debug_mode(capsys):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(
        text='{"workCategory":{"value":"REPAIR","status":"CONFIRMED"}}',
        usage_metadata=None,
    )
    provider = GeminiGraphTestProvider("unit-test-only", "unit-test-model", debug_extraction=True)
    with patch("google.genai.Client", return_value=client):
        output = provider._generate("extract", ["온수가 안 나와요"])

    assert output.values == []
    stderr = capsys.readouterr().err
    assert "[Gemini extraction: response]" in stderr
    assert "[Gemini extraction: parsed]" in stderr
    assert '"values": []' in stderr
    assert "unit-test-only" not in stderr
    client.close.assert_called_once()


def test_gemini_observations_reach_conflict_policy_after_question_limit():
    provider = GeminiGraphTestProvider("unit-test-only", "unit-test-model")
    output = ModelOutput(
        values=[
            ExtractedValue(fieldKey="manufacturer", value="경동나비엔", observedText="경동나비엔"),
            ExtractedValue(
                fieldKey="symptom", value="온수가 안 나와요", observedText="온수가 안 나와요"
            ),
        ],
        intent="ASK_OWNER",
        workCategory=CategoryDecision(value="REPAIR", status="CONFIRMED"),
    )
    request = AnalyzeRequest.model_validate(
        {
            "requestId": "gemini-extraction-test",
            "inquiryId": 101,
            "ownerId": 7,
            "inputVersion": 1,
            "handlerMode": "AI",
            "safetyFlag": False,
            "business": {"activeWorkCategories": ["REPAIR"], "autoBookingEnabled": True},
            "fields": [
                {
                    "fieldKey": "manufacturer",
                    "value": "귀뚜라미",
                    "source": "CUSTOMER",
                    "status": "CONFIRMED",
                }
            ],
            "askCounts": {"manufacturer": 3},
            "messages": [
                {
                    "messageId": 900,
                    "sender": "CUSTOMER",
                    "content": "경동나비엔 보일러인데 온수가 안 나와요. 수리받고 싶어요.",
                    "createdAt": "2026-11-02",
                }
            ],
            "photos": [],
        }
    )
    with patch.object(provider, "_generate", return_value=output):
        state = build_workflow(provider, load_rules()).invoke(
            {
                "request": request,
                "observations": [],
            }
        )

    result = state["result"]
    assert len(state["observations"]) == 2
    assert result.conflicts[0].fieldKey == "manufacturer"
    assert any(f.fieldKey == "symptom" and f.value == "온수가 안 나와요" for f in result.facts)
    assert any(
        f.fieldKey == "manufacturer" and f.unconfirmedReason == "CONFLICT" for f in result.facts
    )
    assert result.intent == "ANSWER"
    assert result.nextAction.action == "CHECK_SCHEDULE"
