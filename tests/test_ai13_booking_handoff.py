"""AI-13 / C-09·C-12·C-13: 예약 진입 계약과 FastAPI 경로를 외부 호출 없이 검증한다."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.intake.contracts import AnalyzeResult, CategoryDecision, Safety
from app.intake.graph import validate_schedule_handoff
from app.intake.providers import ExtractedValue, ModelOutput
from app.intake.rules import load_rules
from app.main import create_app
from tests.bedrock_fakes import sdk_client, sdk_session
from tests.test_intake_graph import FakeProvider, payload, run, symptom


@pytest.mark.parametrize(
    "change",
    [
        {"safety": {"flag": True, "reasons": ["안전 확인 필요"]}},
        {"intent": "ASK_OWNER"},
        {"intent": "CALLBACK"},
        {"workCategory": {"value": None, "status": "UNCONFIRMED"}},
        {"workCategory": {"value": "REPAIR", "status": "UNCONFIRMED"}},
        {"workCategory": {"value": None, "status": "CONFIRMED"}},
    ],
)
def test_inconsistent_schedule_result_is_rejected(change):
    data = run(payload(), FakeProvider(text_facts=[symptom()])).model_dump(mode="json")
    data.update(change)
    with pytest.raises(ValidationError):
        AnalyzeResult.model_validate(data)


def test_schedule_result_cannot_also_request_more_fields():
    data = run(payload(), FakeProvider(text_facts=[symptom()])).model_dump(mode="json")
    data["nextAction"]["targetFieldKeys"] = ["symptom"]
    with pytest.raises(ValidationError, match="추가 확인 항목"):
        AnalyzeResult.model_validate(data)


@pytest.mark.parametrize("blocker", ["owner", "safety", "disabled", "unsupported", "missing"])
def test_final_guard_rejects_schedule_for_changed_request(blocker):
    req = payload()
    result = run(req, FakeProvider(text_facts=[symptom()]))
    if blocker == "owner":
        req.handlerMode = "OWNER"
    elif blocker == "safety":
        req.safetyFlag = True
    elif blocker == "disabled":
        req.business.autoBookingEnabled = False
    elif blocker == "unsupported":
        req.business.activeWorkCategories = []
    else:
        result.facts = []
    with pytest.raises(ValueError, match="자동 예약 연결 불가"):
        validate_schedule_handoff(req, result, load_rules())


def test_final_guard_uses_work_specific_requirements():
    req = payload()
    result = run(req, FakeProvider(text_facts=[symptom()]))
    rules = load_rules()
    rules.conditionalRequiredFieldsByWorkCategory["REPAIR"] = ["model"]
    with pytest.raises(ValueError, match="필수정보"):
        validate_schedule_handoff(req, result, rules)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("ready", "CHECK_SCHEDULE"),
        ("reference_unknown", "CHECK_SCHEDULE"),
        ("missing", "ASK_MORE_INFO"),
        ("missing_limit", "ESCALATE_TO_OWNER"),
        ("required_conflict", "ASK_MORE_INFO"),
        ("category_unknown", "ASK_MORE_INFO"),
        ("disabled", "ESCALATE_TO_OWNER"),
        ("unsupported", "ESCALATE_TO_OWNER"),
        ("stored_safety", "ESCALATE_TO_OWNER"),
        ("model_safety", "ESCALATE_TO_OWNER"),
        ("direct_contact", "ESCALATE_TO_OWNER"),
        ("callback", "ESCALATE_TO_OWNER"),
    ],
)
def test_fastapi_booking_handoff_uses_real_graph(monkeypatch, case, expected):
    monkeypatch.setenv("INTERNAL_KEY", "ai13-test-key")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "ai13-test-model")
    req = payload(
        fields=[
            {
                "fieldKey": "symptom",
                "value": "난방이 안 돼요",
                "source": "CUSTOMER",
                "status": "CONFIRMED",
                "messageId": 900,
            }
        ]
    )
    output = ModelOutput(workCategory=CategoryDecision(value="REPAIR", status="CONFIRMED"))
    if case == "reference_unknown":
        req.fields.append(
            req.fields[0].model_copy(
                update={
                    "fieldKey": "model",
                    "value": None,
                    "status": "UNCONFIRMED",
                    "unconfirmedReason": "CUSTOMER_UNSURE",
                }
            )
        )
    elif case in {"missing", "missing_limit"}:
        req.fields = []
        if case == "missing_limit":
            req.askCounts = {"symptom": 2}
    elif case == "required_conflict":
        req.fields[0].value = "온수가 안 나와요"
        req.fields[0].messageId = 899
        output.values = [
            ExtractedValue(
                fieldKey="symptom", value="난방이 안 돼요", observedText="난방이 안 돼요"
            )
        ]
    elif case == "category_unknown":
        output.workCategory = CategoryDecision(value=None, status="UNCONFIRMED")
    elif case == "disabled":
        req.business.autoBookingEnabled = False
    elif case == "unsupported":
        req.business.activeWorkCategories = ["INSPECTION"]
    elif case == "stored_safety":
        req.safetyFlag = True
    elif case == "model_safety":
        output.safety = Safety(flag=True, reasons=["안전 확인 필요"])
    elif case == "direct_contact":
        req.messages[0].content = "사장님 연결해주세요"
    elif case == "callback":
        req.messages[0].content = "전화 주세요"
    sdk = sdk_client(output)
    with patch("boto3.session.Session", return_value=sdk_session(sdk)):
        response = TestClient(create_app()).post(
            "/internal/v1/inquiries/analyze",
            json=req.model_dump(mode="json"),
            headers={"X-Internal-Key": "ai13-test-key"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] and body["code"] == "OK"
    result = AnalyzeResult.model_validate(body["result"])
    assert result.nextAction.action == expected
    assert (result.requestId, result.inquiryId, result.inputVersion) == (
        req.requestId,
        req.inquiryId,
        req.inputVersion,
    )
    if case in {"stored_safety", "direct_contact", "callback"}:
        sdk.converse.assert_not_called()
    else:
        sdk.converse.assert_called_once()
    assert (
        not {"startAt", "endAt", "workMinutes", "reservationId", "bookingStage"}
        & body["result"].keys()
    )


def test_repeated_analysis_echoes_input_and_does_not_own_reservation_state():
    req = payload()
    first = run(req, FakeProvider(text_facts=[symptom()]))
    repeated = run(req, FakeProvider(text_facts=[symptom()]))
    assert first.model_dump() == repeated.model_dump()
    req.inputVersion += 1
    latest = run(req, FakeProvider(text_facts=[symptom()]))
    assert latest.inputVersion == first.inputVersion + 1
    assert latest.nextAction.action == "CHECK_SCHEDULE"
    # 비교할 DB가 없는 AI는 오래된 결과를 버리지 않는다. Spring이 적용 시점에 검사한다.
    assert first.inputVersion != latest.inputVersion
