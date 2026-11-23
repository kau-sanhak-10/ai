"""AI-05 / B-15·B-16: 실제 Python 프로세스 재기동과 Spring 모의 결과 적용 경계.

DB·문의 전환·예약 멱등성의 실제 Spring 통합 시험을 대신하지 않는다.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.intake.contracts import AnalyzeResult, ExistingField
from simulate_ilro_multiturn import SimulatedSpring
from tests.test_intake_graph import FakeProvider, run

ROOT = Path(__file__).resolve().parents[1]


def invoke_new_process(request, *, output=None, error=None):
    # stdin/stdout으로 합성 상태만 전달한다. 키·실제 대화·.env를 전달하지 않는다.
    completed = subprocess.run(
        [sys.executable, "-m", "tests.ai05_process_worker"],
        input=json.dumps(
            {
                "request": request.model_dump(mode="json"),
                "output": output
                if output is not None
                else {"workCategory": {"value": "REPAIR", "status": "CONFIRMED"}},
                "error": error,
            }
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)


def ready_inquiry():
    db = SimulatedSpring(ask_counts={"symptom": 1})
    message = db.add_customer_message("사장님이 확인한 내용으로 수리를 진행해 주세요")
    db.fields["symptom"] = ExistingField(
        fieldKey="symptom",
        value="사장님 확인 완료",
        source="OWNER",
        status="CONFIRMED",
        messageId=message.messageId,
    )
    return db


def test_ai_process_restart_reuses_latest_state_and_duplicate_reply_is_not_applied():
    db = ready_inquiry()
    request = db.make_request()
    first = invoke_new_process(request)
    assert first["status"] == 200
    result = AnalyzeResult.model_validate(first["body"]["result"])
    assert result.nextAction.action == "CHECK_SCHEDULE"
    assert result.facts == []
    assert db.apply_result(result) == "APPLIED"
    stored = db.snapshot()

    # AI가 결과를 반환한 뒤 전송 중 끊겨 같은 요청이 재전송된 상황이다.
    restarted = invoke_new_process(request)
    assert restarted["pid"] != first["pid"]
    assert restarted["status"] == 200
    repeated = AnalyzeResult.model_validate(restarted["body"]["result"])
    assert repeated.nextAction.action == "CHECK_SCHEDULE"
    assert db.apply_result(repeated) == "DUPLICATE"
    assert db.snapshot() == stored
    assert db.ask_counts == {"symptom": 1}


def test_restart_after_failed_call_preserves_state_until_valid_result_arrives():
    db = ready_inquiry()
    request = db.make_request()
    before = db.snapshot()
    failed = invoke_new_process(request, error="ModelTimeoutException")
    assert failed["status"] == 504
    assert failed["body"]["result"] is None
    assert failed["modelCalls"] == 1
    assert db.snapshot() == before

    restarted = invoke_new_process(request)
    assert restarted["pid"] != failed["pid"]
    assert restarted["status"] == 200
    assert restarted["modelCalls"] == 1
    result = AnalyzeResult.model_validate(restarted["body"]["result"])
    assert db.apply_result(result) == "APPLIED"
    assert result.nextAction.action == "CHECK_SCHEDULE"
    assert db.ask_counts == {"symptom": 1}


@pytest.mark.parametrize("change", ["new_input", "handoff", "handoff_then_resume"])
def test_delayed_result_does_not_overwrite_latest_state_after_transition(change):
    db = ready_inquiry()
    result = run(db.make_request(), FakeProvider(work_category="REPLACEMENT"))
    if change == "new_input":
        db.add_customer_message("수리가 아니라 교체를 선택했어요")
    else:
        db.handler_mode = "OWNER"
        db.input_version += 1
        if change == "handoff_then_resume":
            db.handler_mode = "AI"
            db.input_version += 1
    before = db.snapshot()
    assert db.apply_result(result) == "STALE"
    assert db.snapshot() == before


def test_resumed_ai_uses_owner_confirmation_and_latest_customer_message():
    db = ready_inquiry()
    db.handler_mode = "OWNER"
    db.input_version += 1
    old_request = db.make_request()
    db.add_customer_message("확인한 증상에 대해 수리 대신 교체를 원해요")
    db.handler_mode = "AI"
    db.input_version += 1
    req = db.make_request()
    result = run(req, FakeProvider(work_category="REPLACEMENT"))
    assert result.workCategory.value == "REPLACEMENT"
    assert result.nextAction.action == "CHECK_SCHEDULE"
    assert result.nextAction.targetFieldKeys == []
    assert result.facts == []
    assert req.inputVersion > old_request.inputVersion
    assert db.apply_result(result) == "APPLIED"
    assert db.fields["symptom"].source == "OWNER"
    assert req.messages[-1].content == "확인한 증상에 대해 수리 대신 교체를 원해요"


def test_restored_safety_state_skips_model_and_prevents_booking():
    db = ready_inquiry()
    db.safety_flag = True
    response = invoke_new_process(db.make_request())
    assert response["status"] == 200
    assert response["modelCalls"] == 0
    result = response["body"]["result"]
    assert result["safety"]["flag"] is True
    assert result["nextAction"]["action"] == "ESCALATE_TO_OWNER"


def test_owner_mode_request_never_runs_model_after_restart():
    db = ready_inquiry()
    db.handler_mode = "OWNER"
    response = invoke_new_process(db.make_request())
    assert response["status"] == 400
    assert response["body"]["code"] == "INVALID_INPUT"
    assert response["modelCalls"] == 0


def test_owner_unconfirmed_required_field_still_cannot_reach_schedule():
    db = ready_inquiry()
    db.fields["symptom"].status = "UNCONFIRMED"
    db.fields["symptom"].unconfirmedReason = "NOT_PROVIDED"
    db.ask_counts["symptom"] = 2
    result = run(db.make_request(), FakeProvider())
    assert result.nextAction.action == "ESCALATE_TO_OWNER"
    assert result.nextAction.targetFieldKeys == ["symptom"]
