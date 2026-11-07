"""AI-02 / C-04·07·08: 문맥, 정정, 질문 상한 및 안전한 운영 기록."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.intake.context import PROMPT_VERSION
from app.intake.contracts import CategoryDecision, Message, Photo
from app.intake.providers import ExtractedValue, ModelOutput, OpenAIExtractionProvider
from app.main import create_app
from simulate_ilro_multiturn import SimulatedSpring
from tests.test_intake_graph import FakeProvider, payload, run, symptom


def message(message_id, content, sender="CUSTOMER"):
    return Message(messageId=message_id, sender=sender, content=content, createdAt="2026-11-06")


def sdk_client(*outputs):
    client = MagicMock()
    client.__aenter__.return_value = client
    client.responses.parse = AsyncMock(
        side_effect=[
            SimpleNamespace(
                output_parsed=output, usage=SimpleNamespace(input_tokens=12, output_tokens=8)
            )
            for output in outputs
        ]
    )
    return client


def test_api_passes_current_state_and_question_context_in_one_model_call(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "test-internal-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    req = payload(
        fields=[
            {
                "fieldKey": "manufacturer",
                "value": "경동나비엔",
                "source": "OWNER",
                "status": "CONFIRMED",
            }
        ],
        ask_counts={"symptom": 1},
    )
    req.messages = [
        message(898, "수리를 받고 싶어요"),
        message(899, "온수가 안 나오나요?", "AI"),
        message(900, "네, 안 나와요"),
    ]
    output = ModelOutput(
        values=[
            ExtractedValue(
                fieldKey="symptom",
                value="온수가 안 나와요",
                observedText="안 나와요",
                messageId=900,
            )
        ],
        workCategory=CategoryDecision(value="REPAIR", status="CONFIRMED"),
    )
    client = sdk_client(output)
    with patch("openai.AsyncOpenAI", return_value=client):
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=req.model_dump(mode="json"),
            headers={"X-Internal-Key": "test-internal-key"},
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["nextAction"]["action"] == "CHECK_SCHEDULE"
    assert result["facts"][0]["evidence"]["messageId"] == 900
    assert result["promptVersion"] == PROMPT_VERSION
    assert result["usage"]["inputTokens"] == 12
    assert client.responses.parse.await_count == 1
    sent = json.loads(client.responses.parse.call_args.kwargs["input"][0]["content"][0]["text"])
    assert sent["fields"][0]["source"] == "OWNER"
    assert sent["askCounts"] == {"symptom": 1}
    assert [m["sender"] for m in sent["messages"]] == ["CUSTOMER", "AI", "CUSTOMER"]
    assert not {"ownerId", "inquiryId", "photos", "name", "phone", "address"} & sent.keys()


@pytest.mark.parametrize("content", ["", "   \n\t"])
def test_blank_messages_are_rejected_before_any_model_call(monkeypatch, content):
    monkeypatch.setenv("INTERNAL_KEY", "test-internal-key")
    body = payload().model_dump(mode="json")
    body["messages"][0]["content"] = content
    with patch("app.api.inquiries.OpenAIExtractionProvider") as factory:
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=body,
            headers={"X-Internal-Key": "test-internal-key"},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
    assert response.json()["result"][0]["field"] == "messages.0.content"
    factory.assert_not_called()


@pytest.mark.parametrize("async_mode", [False, True])
def test_both_provider_paths_receive_latest_structured_state(async_mode):
    provider = OpenAIExtractionProvider("test-key", "test-model")
    req = payload(
        fields=[
            {
                "fieldKey": "symptom",
                "value": "온수가 안 나와요",
                "source": "CUSTOMER",
                "status": "CONFIRMED",
                "messageId": 899,
            }
        ]
    )
    output = ModelOutput(workCategory=CategoryDecision(value="REPAIR", status="CONFIRMED"))
    if async_mode:
        with patch.object(provider, "_aparse", new=AsyncMock(return_value=output)) as parse:
            asyncio.run(provider.aextract_request(req))
    else:
        with patch.object(provider, "_parse", return_value=output) as parse:
            provider.extract_request(req)
    sent = json.loads(parse.call_args.args[1][0]["text"])
    assert sent["fields"][0]["value"] == "온수가 안 나와요"


@pytest.mark.parametrize("evidence_id,quote", [(899, "온수"), (900, "없는 내용"), (999, "온수")])
def test_ai_question_or_mismatching_message_cannot_be_customer_evidence(evidence_id, quote):
    provider = OpenAIExtractionProvider("test-key", "test-model")
    output = ModelOutput(
        values=[
            ExtractedValue(
                fieldKey="symptom",
                value="온수가 안 나와요",
                observedText=quote,
                messageId=evidence_id,
            )
        ]
    )
    result = provider._message_output(
        output,
        [
            message(899, "온수 상태를 알려주세요", "AI"),
            message(900, "온수요"),
        ],
    )
    assert result.observations == []


def correction_request(source="CUSTOMER", *, correction_id=900, explicit=True, flag=True):
    old = {
        "fieldKey": "symptom",
        "value": "난방이 안 돼요",
        "source": source,
        "status": "CONFIRMED",
    }
    old["photoId" if source == "PHOTO" else "messageId"] = 898
    latest = "잘못 말했어요. 난방은 나오고 온수가 안 나와요" if explicit else "온수가 안 나와요"
    req = payload(
        fields=[old],
        messages=[
            message(898, "난방이 안 돼요").model_dump(),
            message(900, latest).model_dump(),
        ],
    )
    observation = {
        "fieldKey": "symptom",
        "value": "온수가 안 나와요",
        "source": "CUSTOMER",
        "evidence": {"messageId": correction_id},
        "observedText": "온수가 안 나와요",
        "isCorrection": flag,
    }
    return req, observation


def test_explicit_customer_correction_replaces_previous_statement_without_reasking():
    req, observation = correction_request()
    result = run(req, FakeProvider(text_facts=[observation]))
    assert result.conflicts == []
    assert result.facts[0].value == "온수가 안 나와요"
    assert result.facts[0].evidence.messageId == 900
    assert result.nextAction.action == "CHECK_SCHEDULE"


@pytest.mark.parametrize(
    "options",
    [
        {"source": "OWNER"},
        {"source": "PHOTO"},
        {"explicit": False},
        {"flag": False},
    ],
)
def test_correction_does_not_bypass_owner_photo_or_provenance_guards(options):
    req, observation = correction_request(**options)
    result = run(req, FakeProvider(text_facts=[observation]))
    assert result.conflicts
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert not result.facts


def test_explicit_retraction_to_unknown_clears_old_confirmed_value():
    req, _ = correction_request()
    req.messages[-1].content = "잘못 말했어요. 실제 증상은 모르겠어요"
    provider = OpenAIExtractionProvider("test-key", "test-model")
    output = ModelOutput(
        values=[
            ExtractedValue(
                fieldKey="symptom",
                value=None,
                observedText="모르겠어요",
                messageId=900,
                isCorrection=True,
                unsure=True,
            )
        ]
    )
    raw = provider._message_output(output, req.messages)
    result = run(req, FakeProvider(text_facts=[v.model_dump() for v in raw.observations]))
    assert result.facts[0].value is None
    assert result.facts[0].unconfirmedReason == "CUSTOMER_UNSURE"
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["symptom"]


def test_old_message_cannot_apply_a_new_correction():
    req, observation = correction_request(correction_id=898)
    result = run(req, FakeProvider(text_facts=[observation]))
    # 이미 저장한 근거를 다시 추출한 값은 무시하고 Spring 현재값을 유지한다.
    assert not result.facts
    assert not result.conflicts


def test_previous_values_do_not_reappear_after_spring_saves_customer_correction():
    req, observation = correction_request()
    first = run(req, FakeProvider(text_facts=[observation]))
    fact = first.facts[0]
    req.fields[0] = req.fields[0].model_copy(
        update={
            "value": fact.value,
            "messageId": fact.evidence.messageId,
        }
    )
    req.messages.append(message(902, "수리해주세요"))
    old_observation = {**symptom(), "evidence": {"messageId": 898}}
    result = run(req, FakeProvider(text_facts=[old_observation, observation]))
    assert result.conflicts == []
    assert not result.facts
    assert result.nextAction.action == "CHECK_SCHEDULE"


@pytest.mark.parametrize(
    "count,expected", [(0, "ASK_MORE_INFO"), (1, "ASK_MORE_INFO"), (2, "ESCALATE_TO_OWNER")]
)
def test_required_field_stops_after_two_followups(count, expected):
    result = run(payload(ask_counts={"symptom": count}), FakeProvider())
    assert result.nextAction.action == expected


def test_reference_conflict_is_unconfirmed_and_does_not_handoff_after_two_followups():
    req = payload(
        fields=[
            {
                "fieldKey": "manufacturer",
                "value": "귀뚜라미",
                "source": "CUSTOMER",
                "status": "CONFIRMED",
            }
        ],
        ask_counts={"manufacturer": 2},
    )
    other = {
        "fieldKey": "manufacturer",
        "value": "경동나비엔",
        "source": "PHOTO",
        "evidence": {"photoId": 56},
    }
    req.photos = [Photo(photoId=56, messageId=900, imageUrl="https://example.org/photo.png")]
    result = run(req, FakeProvider(text_facts=[symptom()], image_facts=[other]))
    assert result.nextAction.action == "CHECK_SCHEDULE"
    fact = next(f for f in result.facts if f.fieldKey == "manufacturer")
    assert fact.status == "UNCONFIRMED" and fact.value is None


def test_text_and_photo_followups_share_one_counter():
    db = SimulatedSpring()
    db.add_customer_message("보일러가 안 돼요")
    for index, action in enumerate(["ASK_MORE_INFO", "REQUEST_IMAGE", "ASK_MORE_INFO"]):
        if index:
            db.add_customer_message("아직 모르겠어요")
        result = run(db.make_request(), FakeProvider())
        result.nextAction.action = action
        assert db.apply_result(result) == "APPLIED"
        assert db.ask_counts["symptom"] == index
    final = run(db.make_request(), FakeProvider())
    assert final.nextAction.action == "ESCALATE_TO_OWNER"


@pytest.mark.parametrize(
    "failure,code",
    [
        (None, "OK"),
        ("invalid", "AI_OUTPUT_INVALID"),
        ("api", "AI_MODEL_ERROR"),
        ("timeout", "AI_MODEL_TIMEOUT"),
    ],
)
def test_audit_records_success_and_failure_without_content_or_secret(
    monkeypatch, caplog, failure, code
):
    monkeypatch.setenv("INTERNAL_KEY", "private-internal-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "private-api-secret")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("INTAKE_TIMEOUT_SECONDS", "0.05")
    output = ModelOutput(workCategory=CategoryDecision(value="REPAIR", status="CONFIRMED"))
    client = sdk_client(output)
    if failure == "invalid":
        client.responses.parse.side_effect = None
        client.responses.parse.return_value = SimpleNamespace(
            output_parsed=None, usage=SimpleNamespace(input_tokens=12, output_tokens=8)
        )
    elif failure == "api":
        client.responses.parse.side_effect = RuntimeError("private-exception-message")
    elif failure == "timeout":

        async def wait(**_):
            await asyncio.Future()

        client.responses.parse.side_effect = wait
    req = payload()
    req.messages[0].content = "private-customer-message"
    with patch("openai.AsyncOpenAI", return_value=client):
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=req.model_dump(mode="json"),
            headers={"X-Internal-Key": "private-internal-secret"},
        )
    assert response.json()["code"] == code
    records = [json.loads(r.message) for r in caplog.records if r.name == "app.intake.audit"]
    assert len(records) == 1
    event = records[0]
    assert event["status"] == code
    assert event["promptVersion"] == PROMPT_VERSION
    assert event["modelName"] == "test-model"
    assert event["modelCalls"] == 1
    assert event["latencyMs"] >= 0
    if failure in (None, "invalid"):
        assert event["inputTokens"] == 12 and event["usageReportedCalls"] == 1
    else:
        assert event["usageReportedCalls"] == 0
    assert "private-" not in caplog.text
    client.__aexit__.assert_awaited_once()
