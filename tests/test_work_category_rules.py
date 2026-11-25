"""교체 대상 검증은 모델의 본체 교체 오분류를 예약 확정 근거로 사용하지 않는다."""

from copy import deepcopy

import pytest

from app.intake.contracts import CategoryDecision, Message
from app.intake.work_category_rules import normalize_work_category


def message(text, *, sender="CUSTOMER", number=1):
    return Message(messageId=number, sender=sender, content=text, createdAt="2026-11-25")


@pytest.mark.parametrize(
    "text",
    [
        "밸브를 교체해 주세요",
        "보일러 펌프만 교체하고 싶어요",
        "배관 교체를 원합니다",
        "조절기를 바꿔주세요",
        "밸브 본체를 교체해 주세요",
    ],
)
def test_part_replacement_never_becomes_boiler_replacement(text):
    category = CategoryDecision(value="REPLACEMENT", status="CONFIRMED")
    result = normalize_work_category(category, [message(text)])
    assert result.value == "REPAIR"
    assert result.status == "CONFIRMED"


@pytest.mark.parametrize(
    "text",
    [
        "보일러를 교체해 주세요",
        "보일러 본체 전체를 교체하고 싶어요",
        "보일러를 새것으로 바꿔주세요",
    ],
)
def test_explicit_boiler_replacement_keeps_category(text):
    category = CategoryDecision(value="REPLACEMENT", status="CONFIRMED")
    assert normalize_work_category(category, [message(text)]) == category


@pytest.mark.parametrize(
    "text",
    [
        "교체를 원해요",
        "단열재 교체를 원해요",
        "보일러 교체는 원치 않아요",
        "보일러를 교체하지 마세요",
        "보일러는 교체 안 해요",
        "밸브 교체와 보일러 전체 교체를 모두 원해요",
    ],
)
def test_unknown_denied_or_mixed_target_is_not_confirmed(text):
    result = normalize_work_category(
        CategoryDecision(value="REPLACEMENT", status="CONFIRMED"), [message(text)]
    )
    assert result.status == "UNCONFIRMED"


def test_latest_explicit_customer_target_wins_and_input_is_preserved():
    category = CategoryDecision(value="REPLACEMENT", status="CONFIRMED")
    messages = [message("보일러 전체를 교체하고 싶어요"), message("밸브만 교체해 주세요", number=2)]
    before = deepcopy(messages)
    assert normalize_work_category(category, messages).value == "REPAIR"
    assert messages == before
    assert category.value == "REPLACEMENT"


def test_short_followup_reuses_customer_target_but_owner_text_is_not_evidence():
    category = CategoryDecision(value="REPLACEMENT", status="CONFIRMED")
    assert (
        normalize_work_category(
            category, [message("보일러를 교체하고 싶어요"), message("네 진행해 주세요", number=2)]
        )
        == category
    )
    assert (
        normalize_work_category(
            category, [message("보일러를 교체하고 싶어요", sender="OWNER")]
        ).status
        == "UNCONFIRMED"
    )


def test_unconfirmed_candidate_is_never_promoted():
    category = CategoryDecision(value="REPLACEMENT", status="UNCONFIRMED")
    assert normalize_work_category(category, [message("보일러를 교체해 주세요")]) == category
