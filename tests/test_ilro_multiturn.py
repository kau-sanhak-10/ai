"""일로 DS-05: Spring DB 모사 3턴 연속 상태 전달 회귀 테스트. 외부 Gemini API 미호출."""

from unittest.mock import MagicMock, patch

import pytest

from app.intake.contracts import (
    AnalyzeResult,
    CategoryDecision,
    RawObservation,
    Understanding,
)
from simulate_ilro_multiturn import DEFAULT_TURNS, SimulatedSpring, run_simulation


def test_multiturn_spring_snapshots_are_forwarded_correctly():
    report = run_simulation(mode="mock")
    assert report["all_passed"] is True
    traces = report["traces"]
    assert [t["request_sent_to_ai"]["inputVersion"] for t in traces] == [1, 2, 3]
    assert [t["request_sent_to_ai"]["inquiryId"] for t in traces] == [101, 101, 101]
    assert [len(t["request_sent_to_ai"]["messages"]) for t in traces] == [1, 3, 5]
    assert traces[0]["request_sent_to_ai"]["askCounts"] == {}
    assert traces[1]["request_sent_to_ai"]["askCounts"] == {"symptom": 1}
    assert traces[2]["request_sent_to_ai"]["askCounts"] == {
        "symptom": 1,
        "work_category": 1,
    }
    assert traces[1]["request_sent_to_ai"]["fields"] == []
    assert any(
        f["fieldKey"] == "symptom" and f["value"] == "온수가 안 나와요"
        for f in traces[2]["request_sent_to_ai"]["fields"]
    )
    assert [t["result"]["nextAction"]["action"] for t in traces] == [
        "ASK_MORE_INFO",
        "ASK_MORE_INFO",
        "CHECK_SCHEDULE",
    ]
    assert any(c.get("test") == "stale_result" and c["status"] == "STALE" for c in report["checks"])


def test_old_and_duplicate_result_not_reapplied():
    report = run_simulation(mode="mock")
    db = SimulatedSpring()
    db.add_customer_message(DEFAULT_TURNS[0])
    first_result = AnalyzeResult.model_validate(report["traces"][0]["result"])
    assert db.apply_result(first_result) == "APPLIED"
    before = db.snapshot()
    assert db.apply_result(first_result) == "DUPLICATE"
    assert db.snapshot() == before
    db.add_customer_message(DEFAULT_TURNS[1])
    before = db.snapshot()
    assert db.apply_result(first_result) == "STALE"
    assert db.snapshot() == before


def extracted(symptom, message_id, *, category=None):
    return Understanding(
        observations=[
            RawObservation(
                fieldKey="symptom",
                value=symptom,
                source="CUSTOMER",
                evidence={"messageId": message_id},
            )
        ]
        if symptom
        else [],
        intent="ANSWER",
        workCategory=CategoryDecision(
            value=category,
            status="CONFIRMED" if category else "UNCONFIRMED",
        ),
    )


def scripted_provider(*outputs):
    provider = MagicMock()
    provider.model = "multiturn-reextraction-test"
    provider.input_tokens = provider.output_tokens = 0
    provider.extract_message.side_effect = outputs
    return provider


@pytest.mark.parametrize(
    "reextracted",
    [
        "온수가 안 나와요.",
        "보일러가 켜지는데 온수가 안나와요.",
        None,
    ],
)
def test_reextracting_previous_message_does_not_reask_confirmed_symptom(reextracted):
    provider = scripted_provider(
        extracted("온수가 안나와요", 901),
        extracted(reextracted, 901, category="REPAIR"),
    )

    def turns():
        yield "보일러가 켜지는데 온수가 안나와요."
        yield "수리를 받고 싶어요."
        raise AssertionError("CHECK_SCHEDULE 이후 다음 고객 발화를 요청하면 안 됩니다")

    with patch("simulate_ilro_multiturn.build_provider", return_value=provider):
        report = run_simulation(mode="gemini", turns=turns())

    assert report["all_passed"]
    first, second = report["traces"]
    assert first["result"]["nextAction"]["targetFieldKeys"] == ["work_category"]
    assert second["result"]["conflicts"] == []
    assert second["result"]["nextAction"]["action"] == "CHECK_SCHEDULE"
    assert second["spring_after_ai_result"]["askCounts"] == {"work_category": 1}
    assert second["spring_after_ai_result"]["fields"]["symptom"]["value"] == "온수가 안나와요"
    assert provider.extract_message.call_count == 2


def test_repeated_symptom_with_new_message_and_spacing_is_not_a_conflict():
    provider = scripted_provider(
        extracted("온수가 안나와요", 901),
        extracted("온수가 안 나와요.", 903, category="REPAIR"),
    )
    with patch("simulate_ilro_multiturn.build_provider", return_value=provider):
        report = run_simulation(
            mode="gemini",
            turns=["온수가 안나와요", "온수가 안 나와요. 수리받고 싶어요."],
        )
    result = report["traces"][1]["result"]
    assert result["conflicts"] == []
    assert result["nextAction"]["action"] == "CHECK_SCHEDULE"


def test_changed_symptom_in_new_message_still_requires_confirmation():
    provider = scripted_provider(
        extracted("온수가 안나와요", 901),
        extracted("온수는 나오고 난방이 안돼요", 903, category="REPAIR"),
    )
    with patch("simulate_ilro_multiturn.build_provider", return_value=provider):
        report = run_simulation(
            mode="gemini",
            turns=["온수가 안나와요", "온수는 나오고 난방이 안돼요"],
        )
    result = report["traces"][1]["result"]
    assert result["conflicts"][0]["fieldKey"] == "symptom"
    assert result["nextAction"]["targetFieldKeys"] == ["symptom"]
    assert result["nextAction"]["action"] == "ASK_MORE_INFO"
