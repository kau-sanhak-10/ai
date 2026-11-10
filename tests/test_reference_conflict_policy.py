"""WBS 10/26: reference-field conflict must not cause owner handoff by itself."""

from app.intake.contracts import (
    AnalyzeRequest,
    CategoryDecision,
    RawObservation,
    Safety,
    Understanding,
)
from app.intake.graph import build_workflow
from app.intake.rules import load_rules


class Provider:
    model = "reference-policy-test"
    input_tokens = 0
    output_tokens = 0

    def __init__(self, *, observed: str, category: str | None = "REPAIR", safety: bool = False):
        self.observed = observed
        self.category = category
        self.safety = safety

    def extract_message(self, messages):
        latest = messages[-1]
        return Understanding(
            observations=[
                RawObservation(
                    fieldKey="manufacturer" if self.observed != "symptom" else "symptom",
                    value=("경동나비엔" if self.observed != "symptom" else "온수가 안 나와요"),
                    source="CUSTOMER",
                    evidence={"messageId": latest.messageId},
                ),
                *(
                    [
                        RawObservation(
                            fieldKey="symptom",
                            value="온수가 안 나와요",
                            source="CUSTOMER",
                            evidence={"messageId": latest.messageId},
                        )
                    ]
                    if self.observed != "symptom"
                    else []
                ),
            ],
            intent="ANSWER",
            workCategory=CategoryDecision(
                value=self.category,
                status="CONFIRMED" if self.category else "UNCONFIRMED",
            ),
            safety=Safety(flag=self.safety, reasons=["SAFETY"] if self.safety else []),
        )

    def analyze_image(self, photo):
        raise AssertionError("사진 경로가 없어야 합니다")


def request(*, field="manufacturer", count=3, category_enabled=True, owner=False):
    return AnalyzeRequest.model_validate(
        {
            "requestId": "reference-test-1",
            "inquiryId": 101,
            "ownerId": 7,
            "inputVersion": 12,
            "handlerMode": "AI",
            "safetyFlag": False,
            "business": {
                "activeWorkCategories": ["REPAIR"],
                "autoBookingEnabled": category_enabled,
            },
            "fields": [
                {
                    "fieldKey": field,
                    "value": "귀뚜라미" if field == "manufacturer" else "난방이 안 돼요",
                    "source": "OWNER" if owner else "CUSTOMER",
                    "status": "CONFIRMED",
                },
            ],
            "askCounts": {field: count},
            "messages": [
                {
                    "messageId": 900,
                    "sender": "CUSTOMER",
                    "content": "경동나비엔 보일러인데 온수가 안 나와요.",
                    "createdAt": "2026-11-02",
                }
            ],
            "photos": [],
        }
    )


def run(req, provider):
    return build_workflow(provider, load_rules()).invoke({"request": req, "observations": []})[
        "result"
    ]


def test_reference_conflict_before_limit_asks_to_confirm():
    result = run(request(count=1), Provider(observed="manufacturer"))
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["manufacturer"]
    assert result.conflicts[0].fieldKey == "manufacturer"


def test_reference_conflict_after_limit_does_not_escalate():
    result = run(request(count=3), Provider(observed="manufacturer"))
    assert result.nextAction.action == "CHECK_SCHEDULE"
    assert result.conflicts[0].fieldKey == "manufacturer"
    assert any(
        field.fieldKey == "manufacturer"
        and field.status == "UNCONFIRMED"
        and field.unconfirmedReason == "CONFLICT"
        for field in result.facts
    )


def test_required_symptom_conflict_after_limit_escalates():
    result = run(request(field="symptom", count=3), Provider(observed="symptom"))
    assert result.nextAction.action == "ESCALATE_TO_OWNER"
    assert result.nextAction.targetFieldKeys == ["symptom"]


def test_reference_conflict_after_limit_does_not_hide_missing_category():
    result = run(request(count=3), Provider(observed="manufacturer", category=None))
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["work_category"]


def test_safety_overrides_reference_conflict():
    result = run(request(count=3), Provider(observed="manufacturer", safety=True))
    assert result.nextAction.action == "ESCALATE_TO_OWNER"
    assert result.nextAction.ruleReason == "SAFETY"


def test_owner_value_not_overwritten_when_reference_conflicts():
    result = run(request(count=3, owner=True), Provider(observed="manufacturer"))
    assert result.nextAction.action == "CHECK_SCHEDULE"
    assert result.conflicts[0].fieldKey == "manufacturer"
    assert not any(field.fieldKey == "manufacturer" for field in result.facts)


def test_reference_field_becomes_blocking_when_work_type_requires_it():
    rules = load_rules()
    per_category = dict(rules.conditionalRequiredFieldsByWorkCategory)
    per_category["REPAIR"] = ["manufacturer"]
    rules = rules.model_copy(update={"conditionalRequiredFieldsByWorkCategory": per_category})
    result = build_workflow(Provider(observed="manufacturer"), rules).invoke(
        {"request": request(count=3), "observations": []}
    )["result"]
    assert result.nextAction.action == "ESCALATE_TO_OWNER"
    assert result.nextAction.targetFieldKeys == ["manufacturer"]
    assert not any(
        field.fieldKey == "manufacturer" and field.status == "UNCONFIRMED" for field in result.facts
    )
