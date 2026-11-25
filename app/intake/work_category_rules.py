"""부품 교체를 보일러 본체 교체로 확정하지 않기 위한 보수적 출력 검증."""

import re

from app.intake.contracts import CategoryDecision, Message

_PART = r"(?:부품|밸브|펌프|센서|배관|조절기|리모컨|열교환기|분배기|기판|점화플러그)(?:본체)?"
_UNIT = r"보일러(?:본체(?:전체)?|전체|기기)?"
_REPLACE = r"(?:교체|바꾸|바꿔)"
_BETWEEN = r"(?:를|을|만|도|는|은|부터|먼저|통째로|통째|전부|새것으로|새걸로|새제품으로)*"
_PART_REPLACEMENT = re.compile(_PART + _BETWEEN + _REPLACE)
_UNIT_REPLACEMENT = re.compile(_UNIT + _BETWEEN + _REPLACE)
_DENIED = re.compile(r"^(?:하)?(?:지않|지마|안하|안해|말아|는아니|가아니|는원치않|할생각없)")


def _has_target(pattern: re.Pattern[str], text: str) -> bool:
    return any(not _DENIED.match(text[match.end() :]) for match in pattern.finditer(text))


def normalize_work_category(
    category: CategoryDecision, messages: list[Message]
) -> CategoryDecision:
    """확정된 REPLACEMENT 후보만 검사한다. 미확인 후보를 임의로 확정하지 않는다.

    최신 고객의 명시적 교체 대상부터 본다. 단순 후속 답변이면 앞선 고객의 대상 근거를
    재사용하며 AI/OWNER 문구는 근거로 쓰지 않는다. 유한한 규칙 밖의 대상은 다시 확인한다.
    """
    if category.value != "REPLACEMENT" or category.status != "CONFIRMED":
        return category
    for message in reversed(messages):
        if message.sender != "CUSTOMER":
            continue
        text = re.sub(r"\s+", "", message.content)
        part = _has_target(_PART_REPLACEMENT, text)
        unit = _has_target(_UNIT_REPLACEMENT, text)
        if part and unit:
            return CategoryDecision(value=None, status="UNCONFIRMED")
        if part:
            return CategoryDecision(value="REPAIR", status="CONFIRMED")
        if unit:
            return category
    return CategoryDecision(value=None, status="UNCONFIRMED")
