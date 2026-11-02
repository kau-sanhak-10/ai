"""고객의 명시적인 사람 연결·전화 회신 요청을 확인하는 최소 규칙.

LLM의 ASK_OWNER/CALLBACK 분류만으로 이관하지 않는다.
사진·매뉴얼·예약 판단은 여기서 수행하지 않는다.
"""

import re

# 띄어쓰기만 정규화한다. 문장 내용을 새로 추론하지 않는다.
_OWNER_REQUEST = re.compile(
    r"(?:사장님|담당자|상담원|직원|사람)"
    r"(?:과|와|랑|이랑|한테|에게|께|하고)?"
    r"(?:직접)?(?:전화)?(?:연결|상담|통화|대화|얘기)"
    r"(?:을|를)?"
    r"(?:하고싶(?:어|습|다)|해주|주세|부탁|원해|원합|가능|할래|해줘|요청)"
    r"|직접(?:연결|상담|통화)(?:을|를)?"
    r"(?:하고싶(?:어|습|다)|해주|주세|부탁|원해|원합|가능|할래|해줘|요청)"
)

_CALLBACK_REQUEST = re.compile(
    r"(?:전화|연락|회신|콜백)(?:을|를|좀|다시|한번)*"
    r"(?:해주|주세|부탁|해줘|원해|원합|받고싶(?:어|습|다)|요청)"
    r"|전화상담(?:을|를)?받고싶(?:어|습|다)"
)


def normalize_customer_intent(
    model_intent: str, latest_message: str, *, has_observations: bool
) -> str:
    """명시 요청만 이관 의도로 인정하고, 근거 없는 모델 예측은 일반 상담으로 돌린다.

    별도 고객 버튼/핸드오프 상태는 Spring의 별도 API가 처리한다.
    """
    compact = re.sub(r"\s+", "", latest_message)
    if _CALLBACK_REQUEST.search(compact):
        return "CALLBACK"
    if _OWNER_REQUEST.search(compact):
        return "ASK_OWNER"
    if model_intent in {"ASK_OWNER", "CALLBACK"}:
        return "ANSWER" if has_observations else "OTHER"
    return model_intent
