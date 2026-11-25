"""AI-05 / C-09·C-10: 기존 요청 계약과 서비스 범위 판단."""

import json
from copy import deepcopy
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.intake.context import make_text_input
from app.intake.contracts import AnalyzeRequest, CategoryDecision, Understanding
from app.intake.providers import ModelOutput
from app.main import create_app
from tests.bedrock_fakes import sdk_client, sdk_session
from tests.test_intake_graph import FakeProvider, payload, run, symptom


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("with_symptom", [False, True])
def test_unprovided_model_category_hands_off_without_closing_inquiry(automatic, with_symptom):
    req = payload(
        business={"activeWorkCategories": ["INSPECTION"], "autoBookingEnabled": automatic}
    )
    result = run(req, FakeProvider(text_facts=[symptom()] if with_symptom else []))
    assert result.nextAction.action == "ESCALATE_TO_OWNER"
    assert result.nextAction.targetFieldKeys == []
    assert "사장님" in result.reply.content
    assert "제공하지 않아" not in result.reply.content


def test_no_active_categories_does_not_allow_booking():
    req = payload(business={"activeWorkCategories": [], "autoBookingEnabled": True})
    assert run(req, FakeProvider(text_facts=[symptom()])).nextAction.action == "ESCALATE_TO_OWNER"


def test_disabled_automatic_booking_keeps_available_work_for_owner():
    req = payload(business={"activeWorkCategories": ["REPAIR"], "autoBookingEnabled": False})
    result = run(req, FakeProvider(text_facts=[symptom()]))
    assert result.nextAction.action == "ESCALATE_TO_OWNER"
    assert "비활성화" in result.nextAction.reason


@pytest.mark.parametrize("urgent", ["safety", "direct_contact"])
def test_safety_and_direct_contact_override_unserviceable(urgent):
    req = payload(business={"activeWorkCategories": [], "autoBookingEnabled": True})
    provider = FakeProvider(safety=urgent == "safety")
    if urgent == "direct_contact":
        req.messages[0].content = "사장님 연결해주세요"
    result = run(req, provider)
    assert result.nextAction.action == "ESCALATE_TO_OWNER"
    assert result.nextAction.ruleReason == ("SAFETY" if urgent == "safety" else "DONE")


def test_unknown_category_is_asked_instead_of_declaring_unserviceable():
    req = payload(business={"activeWorkCategories": [], "autoBookingEnabled": True})
    result = run(req, FakeProvider(text_facts=[symptom()], work_category=None))
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["work_category"]


def test_unconfirmed_category_candidate_is_not_promoted_to_confirmed():
    provider = FakeProvider(text_facts=[symptom()])
    raw = provider.extract_message(payload().messages)
    raw.workCategory.status = "UNCONFIRMED"
    with patch.object(provider, "extract_message", return_value=Understanding.model_validate(raw)):
        result = run(payload(), provider)
    assert result.workCategory.value == "REPAIR"
    assert result.workCategory.status == "UNCONFIRMED"
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["work_category"]


def test_original_request_infers_category_without_mutating_input():
    req = payload()
    before = deepcopy(req.model_dump())
    result = run(req, FakeProvider(text_facts=[symptom()]))
    assert result.workCategory.value == "REPAIR"
    assert result.workCategory.status == "CONFIRMED"
    assert result.nextAction.action == "CHECK_SCHEDULE"
    assert req.model_dump() == before


def test_extracted_category_uses_its_own_conditional_required_fields():
    from app.intake.graph import build_workflow
    from app.intake.rules import load_rules

    req = payload(
        business={"activeWorkCategories": ["REPAIR", "REPLACEMENT"], "autoBookingEnabled": True}
    )
    req.messages[0].content = "난방이 안 돼서 보일러 본체를 교체하고 싶어요"
    rules = load_rules()
    rules.conditionalRequiredFieldsByWorkCategory["REPLACEMENT"] = ["model"]
    provider = FakeProvider(text_facts=[symptom()], work_category="REPLACEMENT")
    result = build_workflow(provider, rules).invoke({"request": req, "observations": []})["result"]
    assert result.workCategory.value == "REPLACEMENT"
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["model"]


def test_extracted_category_is_checked_against_latest_business_settings():
    req = payload()
    req.messages[0].content = "난방이 안 돼서 보일러 본체를 교체하고 싶어요"
    result = run(req, FakeProvider(text_facts=[symptom()], work_category="REPLACEMENT"))
    assert result.nextAction.action == "ESCALATE_TO_OWNER"


@pytest.mark.parametrize("value", [None, "REPAIR", "invented-type"])
def test_unagreed_request_extension_is_rejected(value):
    body = payload().model_dump()
    body["currentWorkCategory"] = value
    with pytest.raises(ValidationError) as caught:
        AnalyzeRequest.model_validate(body)
    assert any(
        error["loc"] == ("currentWorkCategory",) and error["type"] == "extra_forbidden"
        for error in caught.value.errors()
    )


def test_owner_confirmation_without_customer_type_still_asks_category():
    req = payload(
        messages=[],
        fields=[
            {
                "fieldKey": "symptom",
                "value": "사장님 확인 완료",
                "source": "OWNER",
                "status": "CONFIRMED",
            }
        ],
    )
    provider = FakeProvider()
    with patch.object(provider, "extract_message", side_effect=AssertionError("호출 금지")):
        result = run(req, provider)
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["work_category"]
    assert result.facts == []


def test_fastapi_and_bedrock_use_original_request_context(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "test-internal")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")
    req = payload(
        business={"activeWorkCategories": ["REPAIR", "REPLACEMENT"], "autoBookingEnabled": True},
        fields=[
            {
                "fieldKey": "symptom",
                "value": "난방이 안 돼요",
                "source": "CUSTOMER",
                "status": "CONFIRMED",
                "messageId": 900,
            }
        ],
    )
    req.messages[0].content = "난방이 안 돼서 보일러 본체 교체를 원해요."
    client = sdk_client(
        ModelOutput(workCategory=CategoryDecision(value="REPLACEMENT", status="CONFIRMED"))
    )
    with patch("boto3.session.Session", return_value=sdk_session(client)):
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=req.model_dump(mode="json"),
            headers={"X-Internal-Key": "test-internal"},
        )
    assert response.status_code == 200
    assert response.json()["result"]["workCategory"] == {
        "value": "REPLACEMENT",
        "status": "CONFIRMED",
    }
    assert response.json()["result"]["nextAction"]["action"] == "CHECK_SCHEDULE"
    sent = json.loads(client.converse.call_args.kwargs["messages"][0]["content"][0]["text"])
    assert set(sent) == {"fields", "askCounts", "messages"}
    assert sent == json.loads(make_text_input(req.messages, req))
    client.converse.assert_called_once()


def test_fastapi_rejects_unagreed_extension_before_model_call(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "test-internal")
    body = payload().model_dump(mode="json")
    body["currentWorkCategory"] = "REPAIR"
    with patch("app.api.inquiries.BedrockExtractionProvider") as factory:
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=body,
            headers={"X-Internal-Key": "test-internal"},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
    assert any(error["field"] == "currentWorkCategory" for error in response.json()["result"])
    factory.assert_not_called()


def test_part_replacement_misclassified_by_model_still_uses_repair_route():
    req = payload()
    req.messages[0].content = "난방이 안 돼요. 보일러 밸브를 교체해 주세요."
    result = run(req, FakeProvider(text_facts=[symptom()], work_category="REPLACEMENT"))
    assert result.workCategory.value == "REPAIR"
    assert result.nextAction.action == "CHECK_SCHEDULE"


def test_replacement_without_target_is_asked_instead_of_booking():
    req = payload(business={"activeWorkCategories": ["REPLACEMENT"], "autoBookingEnabled": True})
    req.messages[0].content = "난방이 안 돼요. 교체를 원해요."
    result = run(req, FakeProvider(text_facts=[symptom()], work_category="REPLACEMENT"))
    assert result.workCategory.status == "UNCONFIRMED"
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["work_category"]
    assert "부품 교체" in result.reply.content and "본체 전체 교체" in result.reply.content


def test_customer_correction_after_unsupported_category_can_reach_schedule():
    req = payload(business={"activeWorkCategories": ["REPAIR"], "autoBookingEnabled": True})
    req.messages[0].content = "난방이 안 돼서 보일러 본체를 교체하고 싶어요"
    previous = run(req, FakeProvider(text_facts=[symptom()], work_category="REPLACEMENT"))
    assert previous.nextAction.action == "ESCALATE_TO_OWNER"
    req.messages.append(
        req.messages[0].model_copy(
            update={"messageId": 901, "content": "보일러 본체가 아니라 밸브만 교체하고 싶어요"}
        )
    )
    req.inputVersion += 1
    corrected = run(req, FakeProvider(text_facts=[symptom()], work_category="REPLACEMENT"))
    assert corrected.workCategory.value == "REPAIR"
    assert corrected.nextAction.action == "CHECK_SCHEDULE"


def test_missing_leak_symptom_asks_about_water_leak_instead_of_heating():
    req = payload(business={"activeWorkCategories": ["LEAK"], "autoBookingEnabled": True})
    result = run(req, FakeProvider(work_category="LEAK"))
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["symptom"]
    assert "누수 증상" in result.reply.content


@pytest.mark.parametrize("value", ["물이 샘", "배관에서 물이 샘", "보일러 하단에서 물이 샘"])
def test_nominal_water_leak_observation_can_proceed(value):
    req = payload(business={"activeWorkCategories": ["LEAK"], "autoBookingEnabled": True})
    original = value.replace("샘", "새요")
    req.messages[0].content = original + ". 누수 수리를 부탁드려요."
    fact = {**symptom(), "value": value, "observedText": original}
    result = run(req, FakeProvider(text_facts=[fact], work_category="LEAK"))
    assert result.facts[0].status == "CONFIRMED"
    assert result.nextAction.action == "CHECK_SCHEDULE"


@pytest.mark.parametrize(
    "value", ["물이 샘이 아닙니다", "물이 샘인지 모르겠어요", "물이 샘플입니다"]
)
def test_denied_or_uncertain_water_leak_is_not_confirmed(value):
    from app.intake.symptom_rules import is_specific_symptom

    assert is_specific_symptom(value) is False
