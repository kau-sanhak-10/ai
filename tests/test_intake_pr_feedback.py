"""PR 피드백: 미확인 증상·연결 의도 근거·사진 처리 이력을 검증한다."""

from unittest.mock import patch

import pytest

from app.intake.contracts import IntentEvidence, Understanding
from app.intake.intent_rules import normalize_customer_intent
from app.intake.providers import ModelOutput, OpenAIExtractionProvider
from app.intake.symptom_rules import is_specific_symptom
from tests.test_intake_graph import FakeProvider, payload, run, symptom
from try_my_intake_gemini import GeminiGraphTestProvider


@pytest.mark.parametrize("value", ["보일러가 고장 났어요", "보일러가 이상해요", "안 돼요"])
def test_vague_symptom_is_unconfirmed_from_first_response(value):
    result = run(payload(), FakeProvider(text_facts=[{**symptom(), "value": value}]))
    fact = result.facts[0]
    assert fact.value == value
    assert fact.status == "UNCONFIRMED"
    assert fact.unconfirmedReason == "NOT_PROVIDED"
    assert result.nextAction.action == "ASK_MORE_INFO"
    assert result.nextAction.targetFieldKeys == ["symptom"]


@pytest.mark.parametrize("previous_status", ["UNCONFIRMED", "CONFIRMED"])
def test_specific_answer_replaces_vague_symptom_without_conflict(previous_status):
    req = payload(
        fields=[
            {
                "fieldKey": "symptom",
                "value": "보일러가 고장 났어요",
                "source": "CUSTOMER",
                "status": previous_status,
                "unconfirmedReason": "NOT_PROVIDED" if previous_status == "UNCONFIRMED" else None,
                "messageId": 899,
            }
        ]
    )
    # 모델이 이전의 모호한 문장과 최신 구체적인 답변을 모두 다시 추출해도 된다.
    observations = [
        {**symptom(), "value": "보일러가 고장 났어요", "evidence": {"messageId": 899}},
        symptom(),
    ]
    result = run(req, FakeProvider(text_facts=observations))
    assert result.conflicts == []
    assert result.facts[0].status == "CONFIRMED"
    assert result.facts[0].value == symptom()["value"]
    assert result.nextAction.action == "CHECK_SCHEDULE"


@pytest.mark.parametrize("adapter", ["openai", "gemini"])
@pytest.mark.parametrize(
    "intent, text",
    [
        ("CALLBACK", "전화 좀 주실 수 있나요"),
        ("ASK_OWNER", "사장님 바꿔주세요"),
    ],
)
def test_grounded_model_intent_survives_regex_miss(adapter, intent, text):
    if adapter == "openai":
        provider = OpenAIExtractionProvider("unit-test", "unit-test-model")
        method = "_parse"
    else:
        provider = GeminiGraphTestProvider("unit-test", "unit-test-model")
        method = "_generate"
    evidence = IntentEvidence(messageId=900, observedText=text)
    output = ModelOutput(intent=intent, intentEvidence=evidence)
    req = payload(
        messages=[
            {
                "messageId": 900,
                "sender": "CUSTOMER",
                "content": text,
                "createdAt": "2026-11-05",
            }
        ]
    )
    with patch.object(provider, method, return_value=output):
        result = run(req, provider)
    assert result.intent == intent
    assert result.intentEvidence == evidence
    assert result.nextAction.action == "ESCALATE_TO_OWNER"


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        IntentEvidence(messageId=899, observedText="안녕하세요"),
        IntentEvidence(messageId=900, observedText="전화 좀 주실 수 있나요"),
        IntentEvidence(messageId=900, observedText=" "),
    ],
)
def test_ungrounded_or_old_intent_is_not_a_handoff(evidence):
    assert (
        normalize_customer_intent(
            "CALLBACK",
            "안녕하세요",
            has_observations=False,
            evidence=evidence,
            message_id=900,
        )
        == "OTHER"
    )


def test_existing_owner_vague_value_is_not_overwritten():
    result = run(
        payload(
            fields=[
                {
                    "fieldKey": "symptom",
                    "value": "고장",
                    "source": "OWNER",
                    "status": "CONFIRMED",
                }
            ]
        ),
        FakeProvider(text_facts=[symptom()]),
    )
    assert not any(f.fieldKey == "symptom" for f in result.facts)
    assert result.conflicts[0].fieldKey == "symptom"


def test_successful_empty_photo_analysis_is_not_repeated():
    req = payload(
        photos=[
            {
                "photoId": 56,
                "messageId": 900,
                "imageUrl": "https://example.org/photo.jpg",
            }
        ]
    )
    provider = FakeProvider(text_facts=[symptom()])
    first = run(req, provider)
    assert first.analyzedPhotoIds == [56]
    assert provider.image_calls == 1
    assert not any(f.source == "PHOTO" for f in first.facts)
    second = run(req.model_copy(update={"processedPhotoIds": first.analyzedPhotoIds}), provider)
    assert provider.image_calls == 1
    assert second.analyzedPhotoIds == []


def test_only_pending_photo_is_analyzed_and_reported():
    req = payload(
        photos=[
            {"photoId": photo_id, "messageId": 900, "imageUrl": "https://example.org/photo.jpg"}
            for photo_id in (56, 57)
        ]
    ).model_copy(update={"processedPhotoIds": [56]})
    provider = FakeProvider(text_facts=[symptom()])
    with patch.object(provider, "analyze_image", return_value=Understanding()) as analyze:
        result = run(req, provider)
    analyze.assert_called_once()
    assert analyze.call_args.args[0].photoId == 57
    assert result.analyzedPhotoIds == [57]


def test_field_evidence_does_not_imply_whole_photo_is_processed():
    req = payload(
        fields=[
            {
                "fieldKey": "model",
                "value": "A-100",
                "source": "PHOTO",
                "status": "CONFIRMED",
                "photoId": 56,
            }
        ],
        photos=[
            {
                "photoId": 56,
                "messageId": 900,
                "imageUrl": "https://example.org/photo.jpg",
            }
        ],
    )
    provider = FakeProvider(text_facts=[symptom()])
    result = run(req, provider)
    assert provider.image_calls == 1
    assert result.analyzedPhotoIds == [56]


@pytest.mark.parametrize(
    "value, specific",
    [
        ("보일러가 고장 났어요", False),
        ("온수가 안 나와요", True),
        ("온수 관련 문의예요", False),
        ("전원이요", False),
        ("보일러가 켜지는데 따뜻한 물이 안 나와요", True),
        ("뜨거운 물이 나오지 않아요", True),
        ("난방 관련 문의입니다", False),
        ("온수 문의예요. 보일러가 이상해요", False),
        ("전원이 안 들어와요", True),
        ("난방은 되고 온수만 안 나와요", True),
    ],
)
def test_symptom_needs_a_target_and_observed_state(value, specific):
    assert is_specific_symptom(value) is specific
    req = payload()
    req.messages[0].content = value
    result = run(req, FakeProvider(text_facts=[{**symptom(), "value": value}]))
    fact = result.facts[0]
    assert fact.value == value
    assert fact.status == ("CONFIRMED" if specific else "UNCONFIRMED")
    if specific:
        assert result.nextAction.action == "CHECK_SCHEDULE"
    else:
        assert fact.unconfirmedReason == "NOT_PROVIDED"
        assert result.nextAction.targetFieldKeys == ["symptom"]


@pytest.mark.parametrize("value", ["온수 관련 문의예요", "전원이요"])
def test_topic_only_symptom_can_be_clarified_without_conflict(value):
    req = payload(
        fields=[
            {
                "fieldKey": "symptom",
                "value": value,
                "source": "CUSTOMER",
                "status": "UNCONFIRMED",
                "unconfirmedReason": "NOT_PROVIDED",
                "messageId": 899,
            }
        ]
    )
    result = run(req, FakeProvider(text_facts=[symptom()]))
    assert result.conflicts == []
    assert result.facts[0].status == "CONFIRMED"
    assert result.nextAction.action == "CHECK_SCHEDULE"


@pytest.mark.parametrize(
    "intent, text",
    [
        ("CALLBACK", "전화 좀 주실 수 있나요?"),
        ("ASK_OWNER", "사장님 바꿔주세요"),
        ("ASK_OWNER", "사장님과 직접 이야기하고 싶어요"),
    ],
)
@pytest.mark.parametrize("grounded", [False, True])
def test_clear_contact_requests_are_not_lost(intent, text, grounded):
    provider = OpenAIExtractionProvider("unit-test", "unit-test-model")
    output = ModelOutput(
        intent=intent if grounded else "OTHER",
        intentEvidence=IntentEvidence(messageId=900, observedText=text) if grounded else None,
    )
    req = payload()
    req.messages[0].content = text
    with patch.object(provider, "_parse", return_value=output):
        result = run(req, provider)
    assert result.intent == intent
    assert result.nextAction.action == "ESCALATE_TO_OWNER"


@pytest.mark.parametrize(
    "text, quote",
    [
        ("전화는 하지 마세요", "전화"),
        ("전화해주지 마세요", "전화"),
        ("사장님과 연결하지 마세요", "사장님"),
        ("사장님이 아까 전화해주셨습니다", "전화"),
        ("사장님과 통화했어요", "사장님"),
        ("전화 안 해주셔도 돼요", "전화"),
        ("사장님이 전화 부탁한다고 하셨어요", "전화"),
        ('안내문에 "전화주세요"라고 적혀 있어요', "전화주세요"),
        ("전화해주지 않았어요", "전화"),
        ("전화 부탁한 적이 있어요", "전화"),
        ("전화해주세요라고 하지 말고 문자로 답해주세요", "전화해주세요"),
    ],
)
@pytest.mark.parametrize("intent", ["ASK_OWNER", "CALLBACK"])
@pytest.mark.parametrize("evidence_kind", ["none", "word", "sentence"])
def test_refusal_and_history_do_not_handoff_even_with_matching_quote(
    text,
    quote,
    intent,
    evidence_kind,
):
    provider = OpenAIExtractionProvider("unit-test", "unit-test-model")
    evidence = (
        None
        if evidence_kind == "none"
        else IntentEvidence(
            messageId=900,
            observedText=quote if evidence_kind == "word" else text,
        )
    )
    req = payload()
    req.messages[0].content = text
    with patch.object(
        provider,
        "_parse",
        return_value=ModelOutput(
            intent=intent,
            intentEvidence=evidence,
        ),
    ):
        result = run(req, provider)
    assert result.intent == "OTHER"
    assert result.intentEvidence is None
    assert result.nextAction.action != "ESCALATE_TO_OWNER"


def test_validated_model_choice_is_preserved_when_request_is_ambiguous():
    # 규칙으로는 사장님 연결과 회신 모두 가능한 요청에서 근거 있는 모델 선택을 유지한다.
    text = "사장님 전화 좀 주세요"
    assert (
        normalize_customer_intent(
            "CALLBACK",
            text,
            has_observations=False,
            message_id=900,
            evidence=IntentEvidence(messageId=900, observedText=text),
        )
        == "CALLBACK"
    )


def test_refusing_call_does_not_hide_separate_owner_request():
    text = "전화는 하지 마세요. 사장님 바꿔주세요."
    provider = OpenAIExtractionProvider("unit-test", "unit-test-model")
    req = payload()
    req.messages[0].content = text
    with patch.object(
        provider,
        "_parse",
        return_value=ModelOutput(
            intent="CALLBACK",
            intentEvidence=IntentEvidence(messageId=900, observedText="전화"),
        ),
    ):
        result = run(req, provider)
    assert result.intent == "ASK_OWNER"
    assert result.intentEvidence is None
    assert result.nextAction.action == "ESCALATE_TO_OWNER"


def test_clause_split_preserves_refusal_before_explicit_owner_request():
    text = "전화하지 말고 사장님 바꿔주세요"
    assert (
        normalize_customer_intent(
            "CALLBACK",
            text,
            has_observations=False,
            message_id=900,
            evidence=IntentEvidence(messageId=900, observedText="전화"),
        )
        == "ASK_OWNER"
    )


def test_failed_photo_batch_can_be_retried_without_cached_partial_ids():
    req = payload(
        photos=[
            {"photoId": photo_id, "messageId": 900, "imageUrl": "https://example.org/photo.jpg"}
            for photo_id in (56, 57)
        ]
    )
    provider = FakeProvider(text_facts=[symptom()])
    with (
        patch.object(
            provider,
            "analyze_image",
            side_effect=[
                Understanding(),
                RuntimeError("photo failed"),
            ],
        ),
        pytest.raises(RuntimeError, match="photo failed"),
    ):
        run(req, provider)
    assert req.processedPhotoIds == []
    retry = FakeProvider(text_facts=[symptom()])
    result = run(req, retry)
    assert result.analyzedPhotoIds == [56, 57]
    assert retry.image_calls == 2
