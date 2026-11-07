"""WBS DS-05: 문자열/사진 Mock으로 분기, 충돌, 미확인, 안전·상태 계약 검증."""

from app.intake.contracts import (
    AnalyzeRequest,
    CategoryDecision,
    Photo,
    RawObservation,
    Safety,
    Understanding,
)
from app.intake.graph import build_workflow
from app.intake.rules import load_rules


class FakeProvider:
    model = "fake-for-tests"
    input_tokens = 0
    output_tokens = 0

    def __init__(self, *, text_facts=None, image_facts=None, work_category="REPAIR", safety=False):
        self.text_facts = text_facts or []
        self.image_facts = image_facts or []
        self.work_category = work_category
        self.safety = safety
        self.image_calls = 0

    def extract_message(self, messages):
        return Understanding(
            observations=[RawObservation(**fact) for fact in self.text_facts],
            intent="ANSWER",
            workCategory=CategoryDecision(
                value=self.work_category,
                status="CONFIRMED" if self.work_category else "UNCONFIRMED",
            ),
            safety=Safety(flag=self.safety, reasons=["SAFETY"] if self.safety else []),
        )

    def analyze_image(self, photo: Photo):
        self.image_calls += 1
        return Understanding(observations=[RawObservation(**fact) for fact in self.image_facts])


def payload(*, fields=None, photos=None, ask_counts=None, messages=None, business=None):
    return AnalyzeRequest.model_validate(
        {
            "requestId": "r-1",
            "inquiryId": 101,
            "ownerId": 7,
            "inputVersion": 12,
            "handlerMode": "AI",
            "safetyFlag": False,
            "business": business
            or {"activeWorkCategories": ["REPAIR"], "autoBookingEnabled": True},
            "fields": fields or [],
            "askCounts": ask_counts or {},
            "messages": messages
            if messages is not None
            else [
                {
                    "messageId": 900,
                    "sender": "CUSTOMER",
                    "content": "수리요, 난방이 안 돼요",
                    "createdAt": "2026-10-20T14:00:00+09:00",
                }
            ],
            "photos": photos or [],
        }
    )


def run(request: AnalyzeRequest, provider: FakeProvider):
    graph = build_workflow(provider, load_rules())
    return graph.invoke({"request": request, "observations": []})["result"]


def symptom():
    return {
        "fieldKey": "symptom",
        "value": "난방이 안 돼요",
        "source": "CUSTOMER",
        "evidence": {"messageId": 900},
        "observedText": "난방이 안 돼요",
    }


def test_normal_text_to_schedule_proposal_without_booking_tool():
    result = run(payload(), FakeProvider(text_facts=[symptom()]))
    assert result.nextAction.action == "CHECK_SCHEDULE"
    assert result.facts[0].fieldKey == "symptom"
    assert result.facts[0].source == "CUSTOMER"
    assert result.inputVersion == 12
    assert result.workCategory.value == "REPAIR"
    assert "예약 확정" not in result.reply.content


def test_reference_model_unreadable_does_not_escalate_or_block():
    provider = FakeProvider(
        text_facts=[symptom()],
        image_facts=[
            {
                "fieldKey": "model",
                "value": None,
                "source": "PHOTO",
                "evidence": {"photoId": 56},
                "unconfirmedReason": "PHOTO_UNREADABLE",
            }
        ],
    )
    result = run(
        payload(
            photos=[
                {"photoId": 56, "messageId": 900, "imageUrl": "https://example.org/temporary.png"}
            ]
        ),
        provider,
    )
    assert provider.image_calls == 1
    assert result.nextAction.action == "CHECK_SCHEDULE"
    assert any(f.fieldKey == "model" and f.status == "UNCONFIRMED" for f in result.facts)


def test_photo_text_conflict_requires_confirmation_not_schedule():
    provider = FakeProvider(
        text_facts=[symptom()],
        image_facts=[
            {
                "fieldKey": "manufacturer",
                "value": "경동나비엔",
                "source": "PHOTO",
                "evidence": {"photoId": 56},
            }
        ],
    )
    result = run(
        payload(
            fields=[
                {
                    "fieldKey": "manufacturer",
                    "value": "귀뚜라미",
                    "source": "CUSTOMER",
                    "status": "CONFIRMED",
                }
            ],
            photos=[
                {"photoId": 56, "messageId": 900, "imageUrl": "https://example.org/temporary.png"}
            ],
        ),
        provider,
    )
    assert result.conflicts[0].fieldKey == "manufacturer"
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["manufacturer"]


def test_reference_conflict_after_question_limit_does_not_escalate():
    provider = FakeProvider(
        text_facts=[symptom()],
        image_facts=[
            {
                "fieldKey": "manufacturer",
                "value": "경동나비엔",
                "source": "PHOTO",
                "evidence": {"photoId": 56},
            }
        ],
    )
    result = run(
        payload(
            fields=[
                {
                    "fieldKey": "manufacturer",
                    "value": "귀뚜라미",
                    "source": "CUSTOMER",
                    "status": "CONFIRMED",
                }
            ],
            ask_counts={"manufacturer": 3},
            photos=[
                {"photoId": 56, "messageId": 900, "imageUrl": "https://example.org/temporary.png"}
            ],
        ),
        provider,
    )
    assert result.nextAction.action == "CHECK_SCHEDULE"
    assert any(
        fact.fieldKey == "manufacturer"
        and fact.status == "UNCONFIRMED"
        and fact.unconfirmedReason == "CONFLICT"
        for fact in result.facts
    )
    assert result.conflicts[0].fieldKey == "manufacturer"


def test_safety_overrides_other_actions():
    result = run(payload(), FakeProvider(text_facts=[symptom()], safety=True))
    assert result.nextAction.action == "ESCALATE_TO_OWNER"
    assert result.safety.flag is True


def test_unknown_work_category_asks_without_forcing_dropdown_selection():
    result = run(payload(), FakeProvider(text_facts=[symptom()], work_category=None))
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["work_category"]


def test_unknown_work_category_after_limit_escalates():
    result = run(
        payload(ask_counts={"work_category": 3}),
        FakeProvider(text_facts=[symptom()], work_category=None),
    )
    assert result.nextAction.action == "ESCALATE_TO_OWNER"


def test_owner_field_not_overwritten_even_when_same_key_proposed():
    result = run(
        payload(
            fields=[
                {
                    "fieldKey": "symptom",
                    "value": "온수 문제",
                    "source": "OWNER",
                    "status": "CONFIRMED",
                }
            ]
        ),
        FakeProvider(text_facts=[symptom()]),
    )
    assert all(f.fieldKey != "symptom" for f in result.facts)
    assert result.conflicts[0].fieldKey == "symptom"


def test_no_photos_skips_image_node():
    provider = FakeProvider(text_facts=[symptom()])
    run(payload(), provider)
    assert provider.image_calls == 0


def test_no_reference_missing_escalation_after_limit():
    result = run(payload(ask_counts={"model": 3}), FakeProvider(text_facts=[symptom()]))
    assert result.nextAction.action == "CHECK_SCHEDULE"


def test_spring_is_source_of_truth_per_request_and_no_checkpoint():
    provider = FakeProvider(text_facts=[symptom()])
    earlier = run(payload(ask_counts={"work_category": 0}), provider)
    later = run(payload(ask_counts={"work_category": 3}), provider)
    assert earlier.inputVersion == later.inputVersion == 12
    assert earlier.nextAction.action == later.nextAction.action == "CHECK_SCHEDULE"


def test_vague_symptom_requires_heating_hotwater_power_clarification():
    vague = {
        "fieldKey": "symptom",
        "value": "보일러가 안 돼요",
        "source": "CUSTOMER",
        "evidence": {"messageId": 900},
    }
    result = run(payload(), FakeProvider(text_facts=[vague]))
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["symptom"]
