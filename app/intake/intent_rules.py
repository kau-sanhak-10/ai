"""고객의 명시적인 사람 연결·전화 회신 요청을 확인하는 최소 규칙.

LLM의 ASK_OWNER/CALLBACK 분류만으로 이관하지 않는다.
사진·매뉴얼·예약 판단은 여기서 수행하지 않는다.
"""

import re

from app.intake.contracts import IntentEvidence

# 근거의 존재뿐 아니라 해당 절이 새 연결 요청인지 확인한다. 부정·과거
# 설명은 보수적으로 제외하고, 다른 절의 명시적인 요청은 계속 확인한다.
_CLAUSE_BREAK = re.compile(r"[.!?。！？,，;；\n]+|하지만|그런데|대신|다만|는데|지만|(?<=말고)")
_REFUSAL = re.compile(
    r"하지마|주지마|하지말|주지말|말아|말았|필요없|원하지않|원치않|싫|사양|거절|"
    r"요청한적없|부탁한적없|하지않|주지않|드리지않|말고|"
    r"안해|안주|안받|않아도|않으셔도|필요하지않|싶지않|싶진않"
)
_PAST_OR_REPORT = re.compile(
    r"했|했었|하셨|해주셨|해줬|주셨|주었|받았|드렸|해주신|"
    r"(?:통화|전화|연락|회신|연결|부탁|요청)한적|전화온|연락온|"
    r"요청하셨|부탁하셨|라고(?:하셨|말|적|써)|예문|문구"
)
_REQUEST = re.compile(
    r"하고싶|받고싶|부탁|요청|원해|원합|원하|바랍|해줘|해주|주세|주시|주실|"
    r"가능(?:할|한|해|합)|할수있|될까요|해도될"
)
_OWNER_TOPIC = re.compile(r"사장님|담당자|상담원|직원|사람|직접")
_CONTACT_ACTION = re.compile(r"연결|상담|통화|대화|얘기|이야기|바꿔|바꾸어|전화(?!번호)")
_CALLBACK_TOPIC = re.compile(r"전화(?!번호)|연락(?!처|번호)|회신|콜백")


def _is_request(clause: str, intent: str) -> bool:
    compact = re.sub(r"\s+", "", clause)
    if _REFUSAL.search(compact) or _PAST_OR_REPORT.search(compact):
        return False
    if not _REQUEST.search(compact):
        return False
    if intent == "ASK_OWNER":
        return bool(_OWNER_TOPIC.search(compact) and _CONTACT_ACTION.search(compact))
    return intent == "CALLBACK" and bool(_CALLBACK_TOPIC.search(compact))


def intent_evidence_matches(
    evidence: IntentEvidence | None, latest_message: str, message_id: int | None
) -> bool:
    return bool(
        evidence is not None
        and evidence.messageId == message_id
        and evidence.observedText.strip()
        and evidence.observedText in latest_message
    )


def intent_evidence_supports(
    model_intent: str,
    evidence: IntentEvidence | None,
    latest_message: str,
    message_id: int | None,
) -> bool:
    if not intent_evidence_matches(evidence, latest_message, message_id):
        return False
    return any(
        (evidence.observedText in clause or clause in evidence.observedText)
        and _is_request(clause, model_intent)
        for part in _CLAUSE_BREAK.split(latest_message)
        if (clause := part.strip())
    )


def normalize_customer_intent(
    model_intent: str,
    latest_message: str,
    *,
    has_observations: bool,
    evidence: IntentEvidence | None = None,
    message_id: int | None = None,
) -> str:
    """명시 요청만 이관 의도로 인정하고, 근거 없는 모델 예측은 일반 상담으로 돌린다.

    별도 고객 버튼/핸드오프 상태는 Spring의 별도 API가 처리한다.
    """
    if model_intent in {"ASK_OWNER", "CALLBACK"} and intent_evidence_supports(
        model_intent, evidence, latest_message, message_id
    ):
        return model_intent
    # 모델의 근거가 없거나 거절/과거 설명일 때도, 별도의 명시 요청은 놓치지 않는다.
    for clause in _CLAUSE_BREAK.split(latest_message):
        if _is_request(clause, "ASK_OWNER"):
            return "ASK_OWNER"
        if _is_request(clause, "CALLBACK"):
            return "CALLBACK"
    if model_intent in {"ASK_OWNER", "CALLBACK"}:
        return "ANSWER" if has_observations else "OTHER"
    return model_intent
