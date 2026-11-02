"""LLM 의도 오분류 방어: 프롬프트 수정 없이 명시 요청과 일반 문의를 구분."""

from app.intake.contracts import (
    AnalyzeRequest,
    CategoryDecision,
    RawObservation,
    Safety,
    Understanding,
)
from app.intake.graph import build_workflow
from app.intake.intent_rules import normalize_customer_intent
from app.intake.rules import load_rules


def test_vague_boiler_failure_is_not_owner_request():
    assert (
        normalize_customer_intent("ASK_OWNER", "보일러가 고장 났어요.", has_observations=True)
        == "ANSWER"
    )


def test_manufacturer_conflict_is_not_owner_request():
    assert (
        normalize_customer_intent(
            "ASK_OWNER",
            "경동나비엔 보일러인데 온수가 안 나와요. 수리받고 싶어요.",
            has_observations=True,
        )
        == "ANSWER"
    )


def test_explicit_owner_request_still_handoffs():
    assert (
        normalize_customer_intent("OTHER", "사장님과 직접 상담하고 싶어요.", has_observations=False)
        == "ASK_OWNER"
    )
    assert (
        normalize_customer_intent("ANSWER", "상담원 연결해 주세요.", has_observations=False)
        == "ASK_OWNER"
    )


def test_explicit_callback_still_handoffs():
    assert normalize_customer_intent("OTHER", "전화 주세요.", has_observations=False) == "CALLBACK"
    assert (
        normalize_customer_intent(
            "ANSWER", "나중에 다시 연락 부탁드립니다.", has_observations=False
        )
        == "CALLBACK"
    )


def test_mention_of_old_call_does_not_handoff():
    assert (
        normalize_customer_intent(
            "ASK_OWNER", "사장님과 통화했어요. 온수가 안 나와요.", has_observations=True
        )
        == "ANSWER"
    )


def test_no_evidence_of_direct_request_and_no_facts():
    assert normalize_customer_intent("CALLBACK", "안녕하세요.", has_observations=False) == "OTHER"


def test_other_intents_remain_unchanged():
    assert (
        normalize_customer_intent("BOOK", "방문 예약하고 싶어요.", has_observations=False) == "BOOK"
    )
    assert normalize_customer_intent("ANSWER", "난방이 안 돼요.", has_observations=True) == "ANSWER"


# 아래 테스트는 실제 LangGraph의 nextAction까지 확인한다. 외부 LLM 호출은 없다.
class MisclassifyingProvider:
    model = "fake-misclassified-intent"
    input_tokens = 0
    output_tokens = 0

    def __init__(self, *, intent: str, category: str | None, facts: list[tuple[str, str]]):
        self.intent = intent
        self.category = category
        self.facts = facts

    def extract_message(self, messages):
        latest = messages[-1]
        return Understanding(
            observations=[
                RawObservation(
                    fieldKey=key,
                    value=value,
                    source="CUSTOMER",
                    evidence={"messageId": latest.messageId},
                    observedText=value,
                )
                for key, value in self.facts
            ],
            intent=self.intent,
            workCategory=CategoryDecision(
                value=self.category,
                status="CONFIRMED" if self.category is not None else "UNCONFIRMED",
            ),
            safety=Safety(flag=False, reasons=[]),
        )

    def analyze_image(self, photo):
        raise AssertionError("문자-only 테스트에서 사진 분석은 호출되면 안 됩니다")


def _request(message: str, *, existing: list[dict] | None = None):
    return AnalyzeRequest.model_validate(
        {
            "requestId": "intent-rule-test-1",
            "inquiryId": 101,
            "ownerId": 7,
            "inputVersion": 1,
            "handlerMode": "AI",
            "safetyFlag": False,
            "business": {"activeWorkCategories": ["REPAIR"], "autoBookingEnabled": True},
            "fields": existing or [],
            "askCounts": {},
            "messages": [
                {
                    "messageId": 900,
                    "sender": "CUSTOMER",
                    "content": message,
                    "createdAt": "2026-10-30",
                }
            ],
            "photos": [],
        }
    )


def test_graph_ambiguous_symptom_asks_more_instead_of_owner():
    provider = MisclassifyingProvider(
        intent="ASK_OWNER",
        category=None,
        facts=[("symptom", "보일러가 고장 났어요.")],
    )
    result = build_workflow(provider, load_rules()).invoke(
        {"request": _request("보일러가 고장 났어요."), "observations": []}
    )["result"]
    assert result.intent == "ANSWER"
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["symptom"]


def test_graph_conflict_asks_reconfirmation_instead_of_owner():
    provider = MisclassifyingProvider(
        intent="ASK_OWNER",
        category="REPAIR",
        facts=[("manufacturer", "경동나비엔"), ("symptom", "온수가 안 나와요")],
    )
    request = _request(
        "경동나비엔 보일러인데 온수가 안 나와요. 수리받고 싶어요.",
        existing=[
            {
                "fieldKey": "manufacturer",
                "value": "귀뚜라미",
                "source": "CUSTOMER",
                "status": "CONFIRMED",
            }
        ],
    )
    result = build_workflow(provider, load_rules()).invoke(
        {"request": request, "observations": []}
    )["result"]
    assert result.intent == "ANSWER"
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["manufacturer"]
    assert result.conflicts[0].fieldKey == "manufacturer"


def test_graph_explicit_direct_contact_still_handoffs():
    provider = MisclassifyingProvider(intent="OTHER", category=None, facts=[])
    request = _request("사장님과 직접 상담하고 싶어요.")
    result = build_workflow(provider, load_rules()).invoke(
        {"request": request, "observations": []}
    )["result"]
    assert result.intent == "ASK_OWNER"
    assert result.nextAction.action == "ESCALATE_TO_OWNER"
