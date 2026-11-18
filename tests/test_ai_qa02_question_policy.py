"""AI-QA-02 / C-07·C-09: 보완 횟수와 결과 적용의 책임 경계를 검증한다."""

from copy import deepcopy
from unittest.mock import patch

import pytest

from app.intake.contracts import ExistingField, PhotoGuidance, Understanding
from app.intake.graph import build_workflow
from app.intake.rules import load_rules
from simulate_ilro_multiturn import SimulatedSpring
from tests.test_intake_graph import FakeProvider, payload, run, symptom


@pytest.mark.parametrize("field", ["symptom", "work_category"])
@pytest.mark.parametrize("count", [None, 0, 1, 2])
def test_followup_boundary_for_each_required_question(field, count):
    req = payload(ask_counts={} if count is None else {field: count})
    before = deepcopy(req.model_dump())
    provider = FakeProvider(
        text_facts=[symptom()] if field == "work_category" else [],
        work_category=None if field == "work_category" else "REPAIR",
    )
    result = run(req, provider)
    assert result.nextAction.action == ("ESCALATE_TO_OWNER" if count == 2 else "ASK_MORE_INFO")
    if count != 2 or field == "symptom":
        assert result.nextAction.targetFieldKeys == [field]
    assert req.model_dump() == before


@pytest.mark.parametrize("count", [None, 0, 1, 2])
def test_confirmed_information_proceeds_without_consuming_remaining_questions(count):
    counts = {} if count is None else {"symptom": count, "work_category": count}
    result = run(payload(ask_counts=counts), FakeProvider(text_facts=[symptom()]))
    assert result.nextAction.action == "CHECK_SCHEDULE"
    assert result.nextAction.targetFieldKeys == []


@pytest.mark.parametrize(
    "field", ["manufacturer", "model", "controller_model", "error_code", "boiler_type"]
)
def test_unconfirmed_reference_at_limit_does_not_block_required_question_or_schedule(field):
    req = payload(
        fields=[
            {
                "fieldKey": field,
                "value": None,
                "source": "CUSTOMER",
                "status": "UNCONFIRMED",
                "unconfirmedReason": "CUSTOMER_UNSURE",
                "messageId": 900,
            }
        ],
        ask_counts={field: 2},
    )
    pending = run(req, FakeProvider())
    assert pending.nextAction.action == "ASK_MORE_INFO"
    assert pending.nextAction.targetFieldKeys == ["symptom"]
    complete = run(req, FakeProvider(text_facts=[symptom()]))
    assert complete.nextAction.action == "CHECK_SCHEDULE"
    assert complete.nextAction.targetFieldKeys == []


@pytest.mark.parametrize("count", [None, 0, 1, 2])
def test_reference_conflict_asks_only_until_followup_limit(count):
    req = payload(
        fields=[
            {
                "fieldKey": "manufacturer",
                "value": "귀뚜라미",
                "source": "CUSTOMER",
                "status": "CONFIRMED",
                "messageId": 899,
            }
        ],
        ask_counts={} if count is None else {"manufacturer": count},
    )
    req.messages[0].content = "경동나비엔 보일러인데 난방이 안 돼요. 수리받고 싶어요"
    observation = {
        "fieldKey": "manufacturer",
        "value": "경동나비엔",
        "source": "CUSTOMER",
        "evidence": {"messageId": 900},
        "observedText": "경동나비엔",
    }
    result = run(req, FakeProvider(text_facts=[symptom(), observation]))
    assert [conflict.fieldKey for conflict in result.conflicts] == ["manufacturer"]
    assert result.nextAction.action == ("CHECK_SCHEDULE" if count == 2 else "ASK_MORE_INFO")
    assert result.nextAction.targetFieldKeys == ([] if count == 2 else ["manufacturer"])
    if count == 2:
        fact = next(fact for fact in result.facts if fact.fieldKey == "manufacturer")
        assert fact.status == "UNCONFIRMED"
        assert fact.unconfirmedReason == "CONFLICT"
        assert fact.value is None


def test_conditionally_required_reference_field_escalates_at_limit():
    rules = load_rules()
    rules.conditionalRequiredFieldsByWorkCategory["REPAIR"] = ["manufacturer"]
    result = build_workflow(FakeProvider(text_facts=[symptom()]), rules).invoke(
        {"request": payload(ask_counts={"manufacturer": 2}), "observations": []}
    )["result"]
    assert result.nextAction.action == "ESCALATE_TO_OWNER"
    assert result.nextAction.targetFieldKeys == ["manufacturer"]


def test_simulated_spring_counts_each_field_and_text_image_requests_together():
    db = SimulatedSpring()
    for field, action, expected in [
        ("symptom", "ASK_MORE_INFO", {"symptom": 0}),
        ("manufacturer", "REQUEST_IMAGE", {"symptom": 0, "manufacturer": 0}),
        ("manufacturer", "ASK_MORE_INFO", {"symptom": 0, "manufacturer": 1}),
        ("symptom", "REQUEST_IMAGE", {"symptom": 1, "manufacturer": 1}),
        ("manufacturer", "REQUEST_IMAGE", {"symptom": 1, "manufacturer": 2}),
        ("symptom", "ASK_MORE_INFO", {"symptom": 2, "manufacturer": 2}),
    ]:
        db.add_customer_message("아직 모르겠어요")
        # REQUEST_IMAGE 결과 적용 계약을 같은 항목의 문자 질문과 교대로 검증한다.
        result = run(db.make_request(), FakeProvider())
        result.nextAction.action = action
        result.nextAction.targetFieldKeys = [field]
        assert db.apply_result(result) == "APPLIED"
        assert db.make_request().askCounts == expected


@pytest.mark.parametrize("action", ["ASK_MORE_INFO", "REQUEST_IMAGE"])
@pytest.mark.parametrize("guard", ["stale", "owner", "duplicate", "wrong_inquiry"])
def test_rejected_question_result_does_not_change_counts_or_stored_state(action, guard):
    db = SimulatedSpring(ask_counts={"manufacturer": 1})
    db.add_customer_message("아직 모르겠어요")
    result = run(db.make_request(), FakeProvider())
    result.nextAction.action = action
    if guard == "stale":
        db.add_customer_message("추가 설명")
    elif guard == "owner":
        db.handler_mode = "OWNER"
    elif guard == "duplicate":
        assert db.apply_result(result) == "APPLIED"
    else:
        result.inquiryId += 1
    before = db.snapshot()
    expected = {"stale": "STALE", "owner": "STALE", "duplicate": "DUPLICATE"}
    assert db.apply_result(result) == expected.get(guard, "REJECT_WRONG_INQUIRY")
    assert db.snapshot() == before


def test_failed_analysis_leaves_snapshot_and_question_counts_unchanged():
    db = SimulatedSpring(ask_counts={"symptom": 1})
    db.add_customer_message("아직 모르겠어요")
    before = db.snapshot()
    provider = FakeProvider()
    with (
        patch.object(provider, "extract_message", side_effect=RuntimeError("model failed")),
        pytest.raises(RuntimeError, match="model failed"),
    ):
        run(db.make_request(), provider)
    assert db.snapshot() == before


class UnreadablePhotoProvider(FakeProvider):
    def analyze_image(self, photo):
        return Understanding(
            photoGuidance=PhotoGuidance(photoId=photo.photoId, target="LABEL", reason="SMALL_TEXT")
        )


@pytest.mark.parametrize("count", [None, 0, 1, 2])
@pytest.mark.parametrize("confirmed", [False, True])
def test_photo_feedback_never_adds_an_untracked_retake_request(count, confirmed):
    req = payload(
        photos=[{"photoId": 56, "messageId": 900, "imageUrl": "https://example.org/photo.png"}],
        ask_counts={} if count is None else {"model": count},
    )
    result = run(req, UnreadablePhotoProvider(text_facts=[symptom()] if confirmed else []))
    assert result.photoGuidance[0].reason == "SMALL_TEXT"
    assert result.nextAction.action == ("CHECK_SCHEDULE" if confirmed else "ASK_MORE_INFO")
    assert result.nextAction.targetFieldKeys == ([] if confirmed else ["symptom"])
    assert "다시 보낼" not in result.reply.content
    assert "찍어 주세요" not in result.reply.content
    assert "글자가 작아" in result.reply.content


def test_non_question_result_preserves_previous_question_counts():
    db = SimulatedSpring(ask_counts={"symptom": 1, "manufacturer": 2})
    latest = db.add_customer_message("난방이 안 돼요. 수리받고 싶어요")
    result = run(
        db.make_request(),
        FakeProvider(text_facts=[{**symptom(), "evidence": {"messageId": latest.messageId}}]),
    )
    assert result.nextAction.action == "CHECK_SCHEDULE"
    assert db.apply_result(result) == "APPLIED"
    assert db.ask_counts == {"symptom": 1, "manufacturer": 2}
    assert db.fields["symptom"] == ExistingField(
        fieldKey="symptom",
        value="난방이 안 돼요",
        source="CUSTOMER",
        status="CONFIRMED",
        messageId=latest.messageId,
    )
