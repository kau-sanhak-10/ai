"""AI-05 실호출 판정기의 거짓 성공 방지. AWS에 접속하지 않는 검사다."""

from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from app.intake.context import PROMPT_VERSION
from app.intake.contracts import AnalyzeRequest
from app.intake.rules import load_rules
from check_ai05_bedrock_live import check_response, main, make_cases
from tests.test_intake_graph import FakeProvider, run, symptom


def sample():
    case = make_cases()[0]
    result = run(
        AnalyzeRequest.model_validate(case["request"]), FakeProvider(text_facts=[symptom()])
    )
    result.modelName = "live-test-model"
    result.usage.inputTokens = 100
    result.usage.outputTokens = 30
    response = {
        "httpStatus": 200,
        "audit": [{"modelCalls": 1}],
        "body": {"success": True, "code": "OK", "result": result.model_dump(mode="json")},
    }
    return case, response


def test_valid_result_requires_real_usage_and_grounded_fact():
    case, response = sample()
    assert all(check_response(case, response, "live-test-model").values())


@pytest.mark.parametrize("field", ["inputTokens", "outputTokens"])
def test_zero_usage_cannot_pass_as_actual_bedrock_call(field):
    case, response = sample()
    response["body"]["result"]["usage"][field] = 0
    assert check_response(case, response, "live-test-model")["real_usage"] is False


@pytest.mark.parametrize("calls", [0, 2])
def test_skipped_or_retried_model_cannot_pass_single_call_case(calls):
    case, response = sample()
    response["audit"] = [{"modelCalls": calls}]
    assert check_response(case, response, "live-test-model")["single_attempt"] is False


def test_live_cases_conform_to_original_request_contract():
    for case in make_cases():
        request = AnalyzeRequest.model_validate(case["request"])
        assert "currentWorkCategory" not in request.model_dump()
    assert "currentWorkCategory" not in AnalyzeRequest.model_json_schema()["properties"]


def test_normal_case_without_symptom_evidence_cannot_pass():
    case, response = sample()
    response["body"]["result"]["facts"] = []
    assert check_response(case, response, "live-test-model")["grounded_symptom"] is False


def test_changed_input_version_cannot_pass():
    case, response = sample()
    response["body"]["result"]["inputVersion"] += 1
    assert check_response(case, response, "live-test-model")["request_identity"] is False


def test_cases_have_bounded_calls_and_same_restart_snapshot():
    cases = make_cases()
    assert len(cases) == 13
    assert sum(c["calls"] for c in cases if not c.get("invalidModel")) == 8
    first = next(c for c in cases if c["name"] == "owner_resume")
    repeated = next(c for c in cases if c["name"] == "owner_restart")
    assert first["request"] == repeated["request"]
    repeated["request"]["inputVersion"] += 1
    assert first["request"] != repeated["request"]


def test_error_response_with_partial_result_or_extra_attempt_cannot_pass():
    case = deepcopy(make_cases()[-1])
    response = {
        "httpStatus": 502,
        "audit": [{"modelCalls": 2}],
        "body": {"success": False, "code": "AI_MODEL_ERROR", "result": {"unexpected": True}},
    }
    checks = check_response(case, response, "invalid-test-model")
    assert checks["no_partial_result"] is False
    assert checks["single_attempt"] is False


def test_live_checks_use_current_versions():
    _, response = sample()
    assert response["body"]["result"]["ruleVersion"] == load_rules().version
    assert response["body"]["result"]["promptVersion"] == PROMPT_VERSION


def test_preflight_closes_sdk_client_without_requiring_context_manager(tmp_path, monkeypatch):
    case, response = sample()
    session = Mock(region_name="ap-northeast-2")
    sts = session.client.return_value
    sts.get_caller_identity.return_value = {"Account": "test-account"}
    monkeypatch.setattr(
        "sys.argv",
        ["check", "--model", "live-test-model", "--save", str(tmp_path / "report.json")],
    )
    with (
        patch("boto3.Session", return_value=session),
        patch("check_ai05_bedrock_live.make_cases", return_value=[case]),
        patch("check_ai05_bedrock_live.run_process", return_value=response),
    ):
        assert main() == 0
    sts.get_caller_identity.assert_called_once_with()
    sts.close.assert_called_once_with()
