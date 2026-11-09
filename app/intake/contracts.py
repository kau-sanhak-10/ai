"""DS-04 6장 Spring ↔ AI JSON 계약. 고객 개인정보·주소는 받지 않는다."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

FieldKey = Literal[
    "manufacturer", "model", "controller_model", "boiler_type", "error_code", "symptom"
]
Source = Literal["CUSTOMER", "PHOTO"]
UnconfirmedReason = Literal[
    "NOT_PROVIDED", "PHOTO_UNREADABLE", "CONFLICT", "CUSTOMER_UNSURE", "ASK_LIMIT_REACHED"
]
Action = Literal[
    "ASK_MORE_INFO",
    "REQUEST_IMAGE",
    "ANSWER_SIDE_QUESTION",
    "CHECK_SCHEDULE",
    "COMPLETE",
    "ESCALATE_TO_OWNER",
    "UNSERVICEABLE",
]
RuleReason = Literal["MISSING_FIELD", "OUT_OF_AREA", "SAFETY", "LOW_CONFIDENCE", "DONE"]
WorkCategory = Literal["INSPECTION", "REPAIR", "LEAK", "REPLACEMENT"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Business(ContractModel):
    activeWorkCategories: list[WorkCategory]
    autoBookingEnabled: bool


class ExistingField(ContractModel):
    fieldKey: FieldKey
    value: str | None
    source: Literal["CUSTOMER", "PHOTO", "OWNER"]
    status: Literal["CONFIRMED", "UNCONFIRMED"]
    unconfirmedReason: UnconfirmedReason | None = None
    photoId: int | None = Field(default=None, gt=0)
    messageId: int | None = Field(default=None, gt=0)


class Message(ContractModel):
    messageId: int = Field(gt=0)
    sender: Literal["CUSTOMER", "AI", "OWNER"]
    content: str
    createdAt: str


class Photo(ContractModel):
    photoId: int = Field(gt=0)
    messageId: int = Field(gt=0)
    imageUrl: str = Field(min_length=1)


class AnalyzeRequest(ContractModel):
    requestId: str = Field(min_length=1)
    inquiryId: int = Field(gt=0)
    ownerId: int = Field(gt=0)
    inputVersion: int = Field(ge=0)
    handlerMode: Literal["AI", "OWNER"]
    business: Business
    fields: list[ExistingField]
    askCounts: dict[str, int] = Field(description="최초 질문을 제외한 항목별 보완 요청 횟수")
    messages: list[Message]
    photos: list[Photo]
    safetyFlag: bool
    # Spring이 성공한 결과를 저장한 뒤 다음 요청에 보내는 처리 이력.
    processedPhotoIds: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def ensure_request_consistency(self):
        if any(photo_id <= 0 for photo_id in self.processedPhotoIds):
            raise ValueError("처리한 사진 ID는 양수여야 합니다")
        if len(set(self.processedPhotoIds)) != len(self.processedPhotoIds):
            raise ValueError("처리한 사진 ID가 중복되었습니다")
        if any(count < 0 for count in self.askCounts.values()):
            raise ValueError("질문 횟수는 음수가 될 수 없습니다")
        if len({msg.messageId for msg in self.messages}) != len(self.messages):
            raise ValueError("메시지 ID가 중복되었습니다")
        if len({photo.photoId for photo in self.photos}) != len(self.photos):
            raise ValueError("사진 ID가 중복되었습니다")
        photo_messages = {photo.messageId for photo in self.photos}
        for index, message in enumerate(self.messages):
            # Spring serializes a photo-only customer message as content="".
            if not message.content.strip() and not (
                message.sender == "CUSTOMER"
                and message.content == ""
                and message.messageId in photo_messages
            ):
                raise ValidationError.from_exception_data(
                    type(self).__name__,
                    [
                        {
                            "type": "value_error",
                            "loc": ("messages", index, "content"),
                            "input": message.content,
                            "ctx": {
                                "error": ValueError("사진이 없는 메시지에는 내용이 필요합니다")
                            },
                        }
                    ],
                )
        if len({item.fieldKey for item in self.fields}) != len(self.fields):
            raise ValueError("현재 저장된 항목 키가 중복되었습니다")
        return self


class Evidence(ContractModel):
    messageId: int | None = Field(default=None, gt=0)
    photoId: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def exactly_one_id(self):
        if (self.messageId is None) == (self.photoId is None):
            raise ValueError("근거는 messageId 또는 photoId 하나만 가질 수 있습니다")
        return self


class Fact(ContractModel):
    fieldKey: FieldKey
    value: str | None
    source: Source
    status: Literal["CONFIRMED", "UNCONFIRMED"]
    unconfirmedReason: UnconfirmedReason | None = None
    evidence: Evidence

    @model_validator(mode="after")
    def validate_fact(self):
        if self.source == "CUSTOMER" and self.evidence.messageId is None:
            raise ValueError("고객 진술은 메시지 근거가 필요합니다")
        if self.source == "PHOTO" and self.evidence.photoId is None:
            raise ValueError("사진 추출은 사진 근거가 필요합니다")
        if self.status == "CONFIRMED" and (not self.value or self.unconfirmedReason):
            raise ValueError("확인된 값은 실제 값이 있어야 합니다")
        if self.status == "UNCONFIRMED" and not self.unconfirmedReason:
            raise ValueError("미확인 값은 미확인 이유가 필요합니다")
        return self


class ConflictCandidate(ContractModel):
    value: str
    source: Literal["CUSTOMER", "PHOTO", "OWNER"]
    evidence: Evidence | None = None


class Conflict(ContractModel):
    fieldKey: FieldKey
    candidates: list[ConflictCandidate] = Field(min_length=2)


class Interpretation(ContractModel):
    kind: Literal["ERROR_CODE_TABLE", "MANUAL"]
    text: str
    reference: dict[str, str]


class CategoryDecision(ContractModel):
    value: WorkCategory | None
    status: Literal["CONFIRMED", "UNCONFIRMED"]


class Safety(ContractModel):
    flag: bool
    reasons: list[str]


class NextAction(ContractModel):
    action: Action
    ruleReason: RuleReason
    targetFieldKeys: list[str]
    reason: str


class Reply(ContractModel):
    content: str = Field(min_length=1)


class Usage(ContractModel):
    latencyMs: int = Field(ge=0)
    inputTokens: int = Field(ge=0)
    outputTokens: int = Field(ge=0)


class IntentEvidence(ContractModel):
    messageId: int = Field(gt=0)
    observedText: str = Field(min_length=1)


class PhotoGuidance(ContractModel):
    photoId: int = Field(gt=0)
    target: Literal["LABEL", "DISPLAY", "UNKNOWN"]
    reason: Literal["TARGET_NOT_VISIBLE", "SMALL_TEXT", "BLUR_LIKELY", "UNREADABLE"]


class AnalyzeResult(ContractModel):
    requestId: str
    inquiryId: int
    inputVersion: int
    ruleVersion: str
    promptVersion: str
    decidedBy: Literal["RULE", "EXTERNAL_API", "SELF_MODEL", "JEV"]
    modelName: str
    usage: Usage
    facts: list[Fact]
    conflicts: list[Conflict]
    interpretations: list[Interpretation]
    workCategory: CategoryDecision
    safety: Safety
    intent: str
    intentEvidence: IntentEvidence | None = None
    analyzedPhotoIds: list[int] = Field(default_factory=list)
    photoGuidance: list[PhotoGuidance] = Field(default_factory=list)
    nextAction: NextAction
    reply: Reply
    confidence: float | None = Field(default=None, ge=0, le=1)


class RawObservation(ContractModel):
    """LLM/VLM이 제안한 후보. 서버 저장값과 비교하기 전까지는 확정 사실이 아니다."""

    fieldKey: FieldKey
    value: str | None
    source: Source
    evidence: Evidence
    unconfirmedReason: UnconfirmedReason | None = None
    observedText: str | None = None
    # 서비스 응답이 아닌 모델 후보의 내부 속성. 최신 고객 원문과 함께 검증한다.
    isCorrection: bool = False


class Understanding(ContractModel):
    observations: list[RawObservation] = Field(default_factory=list)
    intent: Literal["ANSWER", "ASK_OWNER", "CALLBACK", "BOOK", "OTHER"] = "OTHER"
    intentEvidence: IntentEvidence | None = None
    photoGuidance: PhotoGuidance | None = None
    workCategory: CategoryDecision = Field(
        default_factory=lambda: CategoryDecision(value=None, status="UNCONFIRMED")
    )
    safety: Safety = Field(default_factory=lambda: Safety(flag=False, reasons=[]))


class ManualHit(ContractModel):
    documentId: str
    manufacturer: str
    model: str | None
    page: int | None
    text: str
