"""실험 판정기가 증상 오해·DB만의 보호를 성공으로 잘못 판정하지 않는지 검증한다."""

from unittest.mock import Mock

from check_ai02_live import check_case, run_case


def trace(*, fields, observations=None, facts=None, conflicts=None, action="CHECK_SCHEDULE"):
    return {
        "apply_status": "APPLIED",
        "spring_after_ai_result": {"fields": fields},
        "node_updates": [
            {
                "node": "extract_message",
                "updates": {
                    "observations": observations or [],
                },
            }
        ],
        "result": {
            "facts": facts or [],
            "conflicts": conflicts or [],
            "nextAction": {"action": action, "targetFieldKeys": ["manufacturer"]},
        },
    }


def test_negation_does_not_pass_when_normal_and_broken_parts_are_swapped():
    wrong = trace(fields={"symptom": {"value": "온수가 안 나와요", "status": "CONFIRMED"}})
    checks = check_case("negation", [wrong])
    assert checks["기대한 증상 확인"] is False
    assert checks["정상인 부분을 고장으로 기록하지 않음"] is False


def test_owner_check_catches_ai_overwrite_even_if_simulated_db_protects_value():
    protected = trace(
        fields={"manufacturer": {"value": "경동나비엔", "source": "OWNER", "status": "CONFIRMED"}},
        observations=[{"fieldKey": "manufacturer", "value": "귀뚜라미"}],
        facts=[{"fieldKey": "manufacturer", "value": "귀뚜라미"}],
        conflicts=[{"fieldKey": "manufacturer"}],
        action="ASK_MORE_INFO",
    )
    checks = check_case("owner", [protected])
    assert checks["OWNER 현재값 유지"] is True
    assert checks["AI가 OWNER 값 변경을 제안하지 않음"] is False


def test_owner_check_requires_customer_contradiction_to_be_extracted():
    protected = trace(
        fields={
            "manufacturer": {
                "value": "경동나비엔",
                "source": "OWNER",
                "status": "CONFIRMED",
            }
        }
    )
    checks = check_case("owner", [protected])
    assert checks["OWNER 현재값 유지"] is True
    assert checks["고객의 다른 제조사도 추출"] is False
    assert not all(checks.values())


def test_call_failure_stops_later_turn_and_never_prints_exception_message(capsys):
    runner = Mock(side_effect=RuntimeError("private-content-or-secret"))
    report = run_case("correction", "test-model", turn_runner=runner)
    assert report["status"] == "API_ERROR"
    assert report["traces"] == []
    assert runner.call_count == 1
    assert "private-content-or-secret" not in capsys.readouterr().out
