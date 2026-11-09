"""DS-05 문의 1회 분석: 8개 Node, 조건부 Edge, 무상태 실행.

Spring DB가 유일한 원본이다. Graph의 State는 요청 동안만 존재한다.
이미지/매뉴얼 모듈 교체는 Node 연결을 변경하지 않는다.
멀티턴에서는 같은 근거의 재추출과 증상의 표기 차이로 확인값을 재질문하지 않는다.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable
from typing import Annotated, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from app.intake.context import PROMPT_VERSION, is_explicit_correction
from app.intake.contracts import (
    AnalyzeRequest,
    AnalyzeResult,
    CategoryDecision,
    Conflict,
    ConflictCandidate,
    Evidence,
    Fact,
    IntentEvidence,
    Interpretation,
    ManualHit,
    NextAction,
    RawObservation,
    Reply,
    Safety,
    Understanding,
    Usage,
)
from app.intake.intent_rules import intent_evidence_supports, normalize_customer_intent
from app.intake.providers import AsyncExtractionProvider, ExtractionProvider
from app.intake.rules import IntakeRules
from app.intake.symptom_rules import is_specific_symptom


class IntakeState(TypedDict, total=False):
    request: AnalyzeRequest
    stage: str
    observations: Annotated[list[RawObservation], operator.add]
    intent: str
    intentEvidence: IntentEvidence | None
    analyzedPhotoIds: list[int]
    workCategory: CategoryDecision
    safety: Safety
    facts: list[Fact]
    conflicts: list[Conflict]
    nextAction: NextAction | None
    manualHits: list[ManualHit]
    interpretations: list[Interpretation]
    reply: Reply
    result: AnalyzeResult


ManualSearch = Callable[[str, str | None, str | None], list[ManualHit]]


def no_manual_results(query: str, manufacturer: str | None, model: str | None) -> list[ManualHit]:
    """자료 연동 전에는 검색 결과가 없다고 명시한다. 절대 출처를 만들어내지 않는다."""
    return []


def effective_fields(state: IntakeState) -> dict[str, str]:
    """DB의 현재 확인값을 기준으로 이번 요청의 비충돌 새 사실만 반영한 읽기 전용 뷰."""
    result = {
        field.fieldKey: field.value
        for field in state["request"].fields
        if field.status == "CONFIRMED"
        and field.value is not None
        and (field.fieldKey != "symptom" or is_specific_symptom(field.value))
    }
    owners = {f.fieldKey for f in state["request"].fields if f.source == "OWNER"}
    conflicted = {conflict.fieldKey for conflict in state.get("conflicts", [])}
    for fact in state.get("facts", []):
        if fact.fieldKey not in owners and fact.status == "UNCONFIRMED":
            result.pop(fact.fieldKey, None)
        if (
            fact.fieldKey not in owners
            and fact.fieldKey not in conflicted
            and fact.status == "CONFIRMED"
            and fact.value is not None
        ):
            result[fact.fieldKey] = fact.value
    for field in conflicted:
        result.pop(field, None)
    return result


def build_workflow(
    provider: ExtractionProvider,
    rules: IntakeRules,
    *,
    manual_search: ManualSearch = no_manual_results,
    asynchronous: bool = False,
):
    """요청별 의존성만 주입한다. checkpointer, 예약·이관 실행 도구는 사용하지 않는다."""

    def message_update(state: IntakeState, raw: Understanding) -> dict:
        customer_messages = [m for m in state["request"].messages if m.sender == "CUSTOMER"]
        if not customer_messages:
            return {
                "stage": "EXTRACT",
                "observations": [],
                "intent": "OTHER",
                "workCategory": CategoryDecision(value=None, status="UNCONFIRMED"),
                "safety": Safety(flag=state["request"].safetyFlag, reasons=[]),
            }
        latest = customer_messages[-1]
        intent = normalize_customer_intent(
            raw.intent,
            latest.content,
            has_observations=bool(raw.observations),
            evidence=raw.intentEvidence,
            message_id=latest.messageId,
        )
        return {
            "stage": "EXTRACT",
            "observations": raw.observations,
            "intent": intent,
            "intentEvidence": raw.intentEvidence
            if intent in {"ASK_OWNER", "CALLBACK"}
            and raw.intent == intent
            and intent_evidence_supports(
                intent, raw.intentEvidence, latest.content, latest.messageId
            )
            else None,
            "workCategory": raw.workCategory,
            "safety": Safety(
                flag=state["request"].safetyFlag or raw.safety.flag,
                reasons=raw.safety.reasons,
            ),
        }

    def extract_message(state: IntakeState) -> dict:
        messages = [
            m for m in state["request"].messages if m.sender == "CUSTOMER" and m.content.strip()
        ]
        if messages and callable(getattr(type(provider), "extract_request", None)):
            raw = provider.extract_request(state["request"])
        else:
            raw = provider.extract_message(messages) if messages else Understanding()
        return message_update(state, raw)

    async def aextract_message(state: IntakeState) -> dict:
        messages = [
            m for m in state["request"].messages if m.sender == "CUSTOMER" and m.content.strip()
        ]
        if messages and callable(getattr(type(provider), "aextract_request", None)):
            raw = await provider.aextract_request(state["request"])
        else:
            raw = (
                await cast(AsyncExtractionProvider, provider).aextract_message(messages)
                if messages
                else Understanding()
            )
        return message_update(state, raw)

    def pending_photos(state: IntakeState):
        processed = set(state["request"].processedPhotoIds)
        return [photo for photo in state["request"].photos if photo.photoId not in processed]

    def needs_image(state: IntakeState) -> str:
        return "analyze_image" if pending_photos(state) else "merge_facts"

    def image_update(state: IntakeState, outputs: list[tuple[int, Understanding | None]]) -> dict:
        observations = []
        safety_reasons = list(state["safety"].reasons)
        has_safety = state["safety"].flag
        for _, raw in outputs:
            if raw is None:
                continue
            observations.extend(raw.observations)
            if raw.safety.flag:
                has_safety = True
                safety_reasons.extend(raw.safety.reasons)
        return {
            "stage": "EXTRACT",
            "observations": observations,
            "analyzedPhotoIds": [photo_id for photo_id, raw in outputs if raw is not None],
            "safety": Safety(flag=has_safety, reasons=list(dict.fromkeys(safety_reasons))),
        }

    def analyze_image(state: IntakeState) -> dict:
        return image_update(
            state,
            [(photo.photoId, provider.analyze_image(photo)) for photo in pending_photos(state)],
        )

    async def aanalyze_image(state: IntakeState) -> dict:
        outputs = []
        for photo in pending_photos(state):
            raw = await cast(AsyncExtractionProvider, provider).aanalyze_image(photo)
            outputs.append((photo.photoId, raw))
        return image_update(state, outputs)

    def merge_facts(state: IntakeState) -> dict:
        originals = {field.fieldKey: field for field in state["request"].fields}
        groups: dict[str, list[RawObservation]] = {}
        for item in state.get("observations", []):
            groups.setdefault(item.fieldKey, []).append(item)
        facts: list[Fact] = []
        conflicts: list[Conflict] = []
        latest_customer = next(
            (m for m in reversed(state["request"].messages) if m.sender == "CUSTOMER"), None
        )
        message_positions = {
            message.messageId: index for index, message in enumerate(state["request"].messages)
        }
        for field_key, observations in groups.items():
            existing = originals.get(field_key)
            correction = next(
                (
                    item
                    for item in reversed(observations)
                    if item.isCorrection
                    and item.source == "CUSTOMER"
                    and latest_customer is not None
                    and item.evidence.messageId == latest_customer.messageId
                    and item.observedText
                    and item.observedText.strip()
                    and item.observedText in latest_customer.content
                    and is_explicit_correction(latest_customer.content)
                ),
                None,
            )
            # 명시적인 정정은 고객의 이전 설명만 대체한다. 사진·OWNER 값은 계속 비교한다.
            replaces_customer = correction is not None and (
                existing is None or existing.source == "CUSTOMER"
            )
            if replaces_customer:
                observations = [correction] + [v for v in observations if v.source == "PHOTO"]
            existing_confirmed = bool(
                existing
                and not replaces_customer
                and existing.status == "CONFIRMED"
                and existing.value
                and (
                    field_key != "symptom"
                    or existing.source == "OWNER"
                    or is_specific_symptom(existing.value)
                )
            )
            confirmed = [
                v
                for v in observations
                if v.value
                and v.value.strip()
                and not v.unconfirmedReason
                and (field_key != "symptom" or is_specific_symptom(v.value))
            ]
            if existing_confirmed:
                # 과거 발화/사진을 다시 요약한 값은 새 고객 입력이 아니다.
                # 같은 근거의 재추출로 Spring의 확인값과 가짜 충돌을 만들지 않는다.
                confirmed = [
                    item
                    for item in confirmed
                    if not (
                        item.source == existing.source
                        and (
                            (
                                item.source == "CUSTOMER"
                                and existing.messageId is not None
                                and (
                                    item.evidence.messageId == existing.messageId
                                    or (
                                        existing.messageId in message_positions
                                        and item.evidence.messageId in message_positions
                                        and message_positions[item.evidence.messageId]
                                        < message_positions[existing.messageId]
                                    )
                                )
                            )
                            or (
                                item.source == "PHOTO"
                                and existing.photoId is not None
                                and item.evidence.photoId == existing.photoId
                            )
                        )
                    )
                ]
            candidates: list[ConflictCandidate] = []
            if existing_confirmed:
                evidence = None
                if existing.photoId is not None and (
                    existing.source == "PHOTO" or existing.messageId is None
                ):
                    evidence = Evidence(photoId=existing.photoId)
                elif existing.messageId is not None:
                    evidence = Evidence(messageId=existing.messageId)
                candidates.append(
                    ConflictCandidate(
                        value=existing.value,
                        source=existing.source,
                        evidence=evidence,
                    )
                )
            candidates.extend(
                ConflictCandidate(value=item.value, source=item.source, evidence=item.evidence)
                for item in confirmed
            )
            unique = {
                # 증상의 공백·문장 끝 구두점 차이는 의미 충돌이 아니다.
                # 제조사/모델/오류코드의 식별 문자는 그대로 비교한다.
                re.sub(r"\s+", "", v.value).rstrip(".!?。！？").lower()
                if field_key == "symptom"
                else v.value.strip().lower()
                for v in candidates
            }
            if len(unique) > 1:
                conflicts.append(Conflict(fieldKey=field_key, candidates=candidates))
                # 10/26: 참고정보 충돌로 질문 한도에 도달한 경우만 미확인 제안.
                # OWNER 출처는 AI가 변경할 수 없고, 필수정보 충돌은 계속 차단한다.
                if (
                    field_key in rules.referenceFields
                    and field_key not in rules.required_fields(state["workCategory"].value)
                    and state["request"].askCounts.get(field_key, -1) >= rules.followupLimit
                    and not (existing and existing.source == "OWNER")
                ):
                    latest = confirmed[-1]
                    facts.append(
                        Fact(
                            fieldKey=field_key,
                            value=None,
                            source=latest.source,
                            status="UNCONFIRMED",
                            unconfirmedReason="CONFLICT",
                            evidence=latest.evidence,
                        )
                    )
                continue
            if existing and existing.source == "OWNER":
                # 사장님 수정값은 어떤 AI 출력으로도 덮어쓰지 않는다.
                continue
            if existing_confirmed:
                continue
            if confirmed:
                item = confirmed[-1]
                facts.append(
                    Fact(
                        fieldKey=field_key,
                        value=item.value,
                        source=item.source,
                        status="CONFIRMED",
                        evidence=item.evidence,
                    )
                )
            else:
                unreadable = next((v for v in reversed(observations) if v.unconfirmedReason), None)
                if unreadable is None and field_key == "symptom":
                    vague = next((v for v in reversed(observations) if v.value), None)
                    if vague is not None:
                        unreadable = vague.model_copy(update={"unconfirmedReason": "NOT_PROVIDED"})
                if unreadable is not None:
                    facts.append(
                        Fact(
                            fieldKey=field_key,
                            value=unreadable.value,
                            source=unreadable.source,
                            status="UNCONFIRMED",
                            unconfirmedReason=unreadable.unconfirmedReason,
                            evidence=unreadable.evidence,
                        )
                    )
        return {"stage": "EXTRACT", "facts": facts, "conflicts": conflicts}

    def check_rules(state: IntakeState) -> dict:
        req = state["request"]
        safety = state["safety"]
        if req.handlerMode != "AI":
            # Spring도 결과 적용 전 handlerMode와 최신 입력을 다시 확인한다.
            raise ValueError("OWNER 모드에서는 AI 자동 분석을 허용하지 않습니다")
        if safety.flag:
            return {
                "stage": "DECIDE",
                "nextAction": NextAction(
                    action="ESCALATE_TO_OWNER",
                    ruleReason="SAFETY",
                    targetFieldKeys=[],
                    reason="안전 문제에 대한 사장님 확인 필요",
                ),
            }
        if state["intent"] in {"ASK_OWNER", "CALLBACK"}:
            return {
                "stage": "DECIDE",
                "nextAction": NextAction(
                    action="ESCALATE_TO_OWNER",
                    ruleReason="DONE",
                    targetFieldKeys=[],
                    reason="고객이 직접 연결 또는 회신을 요청함",
                ),
            }
        return {"stage": "DECIDE", "nextAction": None}

    def choose_route(state: IntakeState) -> str:
        return "generate_reply" if state.get("nextAction") is not None else "decide_action"

    def decide_action(state: IntakeState) -> dict:
        req = state["request"]
        effective = effective_fields(state)
        category = state["workCategory"]
        required = rules.required_fields(category.value)
        conflicted = {v.fieldKey for v in state.get("conflicts", [])}
        missing = sorted(f for f in required if not effective.get(f))
        # 업무별 필수정보와 참고정보를 구분해 다음 질문을 선정한다.
        # 알 수 없는 유형의 충돌도 참고정보로 추정하지 않고 안전하게 보류한다.
        blocking_conflicts = sorted(
            key for key in conflicted if key in required or key not in rules.referenceFields
        )
        reference_conflicts = sorted(conflicted - set(blocking_conflicts))
        target = blocking_conflicts[0] if blocking_conflicts else None
        if target is None and missing:
            target = missing[0]
        # 필수 증상이 충분한지 확인한 뒤, 참고정보 충돌은 남은 질문 횟수 안에서만 묻는다.
        # 한도를 넘긴 참고정보만으로 ESCALATE_TO_OWNER를 생성하지 않는다.
        if target is None:
            target = next(
                (
                    key
                    for key in reference_conflicts
                    if req.askCounts.get(key, -1) < rules.followupLimit
                ),
                None,
            )
        if target is not None:
            count = req.askCounts.get(target, -1)
            if count >= rules.followupLimit:
                action = NextAction(
                    action="ESCALATE_TO_OWNER",
                    ruleReason="LOW_CONFIDENCE",
                    targetFieldKeys=[target],
                    reason="필수정보 또는 정보 충돌 재확인 한도에 도달함",
                )
            else:
                action = NextAction(
                    action="ASK_MORE_INFO",
                    ruleReason="MISSING_FIELD",
                    targetFieldKeys=[target],
                    reason="미확인 또는 충돌 항목 재확인 필요",
                )
            return {"stage": "DECIDE", "nextAction": action}
        # 고객의 작업 유형 선택은 선택적이다. 예약으로 넘기려면 대화에서라도 확인돼야 한다.
        latest_customer = next(
            (m.content for m in reversed(req.messages) if m.sender == "CUSTOMER"), ""
        )
        if state["intent"] == "ANSWER" and any(
            term in latest_customer for term in ("무슨 뜻", "무슨 의미", "설명서", "오류 뜻")
        ):
            return {
                "stage": "DECIDE",
                "nextAction": NextAction(
                    action="ANSWER_SIDE_QUESTION",
                    ruleReason="DONE",
                    targetFieldKeys=[],
                    reason="고객의 매뉴얼 관련 부가질문",
                ),
            }
        if category.status != "CONFIRMED" or category.value is None:
            if req.askCounts.get("work_category", -1) >= rules.followupLimit:
                return {
                    "stage": "DECIDE",
                    "nextAction": NextAction(
                        action="ESCALATE_TO_OWNER",
                        ruleReason="LOW_CONFIDENCE",
                        targetFieldKeys=[],
                        reason="작업 유형을 끝내 확인할 수 없음",
                    ),
                }
            return {
                "stage": "DECIDE",
                "nextAction": NextAction(
                    action="ASK_MORE_INFO",
                    ruleReason="MISSING_FIELD",
                    targetFieldKeys=["work_category"],
                    reason="작업 유형을 대화로 확인할 필요가 있음",
                ),
            }
        if (
            category.value not in req.business.activeWorkCategories
            or not req.business.autoBookingEnabled
        ):
            return {
                "stage": "DECIDE",
                "nextAction": NextAction(
                    action="ESCALATE_TO_OWNER",
                    ruleReason="DONE",
                    targetFieldKeys=[],
                    reason="업체의 자동 예약 설정 또는 제공 작업 유형 확인 필요",
                ),
            }
        return {
            "stage": "DECIDE",
            "nextAction": NextAction(
                action="CHECK_SCHEDULE",
                ruleReason="DONE",
                targetFieldKeys=[],
                reason="현재 AI 수집 단계가 끝남. 지역·인증·일정은 Spring이 최종 검사",
            ),
        }

    def needs_manual(state: IntakeState) -> str:
        # 해당 자료의 검색/해석은 부가질문에만 제공한다. 예약 필수 항목에 추가하지 않는다.
        if state.get("nextAction") and state["nextAction"].action == "ANSWER_SIDE_QUESTION":
            return "search_manual"
        return "generate_reply"

    def search_manual(state: IntakeState) -> dict:
        fields = effective_fields(state)
        manufacturer = fields.get("manufacturer")
        model = fields.get("model")
        if not manufacturer or not model:
            return {"stage": "DECIDE", "manualHits": []}
        latest = next(
            (m.content for m in reversed(state["request"].messages) if m.sender == "CUSTOMER"), ""
        )
        hits = manual_search(latest, manufacturer, model)
        # 다른 제조사/모델의 매뉴얼을 섞지 않는다.
        valid = [hit for hit in hits if hit.manufacturer == manufacturer and hit.model == model]
        return {"stage": "DECIDE", "manualHits": valid}

    def generate_reply(state: IntakeState) -> dict:
        action = state["nextAction"]
        assert action is not None
        target = action.targetFieldKeys[0] if action.targetFieldKeys else None
        labels = {
            "manufacturer": "보일러 제조사",
            "model": "보일러 본체 모델명",
            "boiler_type": "보일러 종류",
            "error_code": "오류코드",
            "symptom": "난방·온수·전원 중 어떤 부분이 작동하지 않는지",
        }
        if action.action == "ASK_MORE_INFO":
            if target == "work_category":
                content = "어떤 작업을 원하시는지 알려주실 수 있을까요? (점검·수리·누수·교체 등)"
            elif target == "symptom":
                content = "난방·온수·전원 중 어떤 부분이 작동하지 않는지 알려주실 수 있을까요?"
            elif target:
                content = f"{labels.get(target, target)}을(를) 확인해 주실 수 있을까요?"
            else:
                content = "어떤 작업을 원하시는지 알려주실 수 있을까요?"
        elif action.action == "REQUEST_IMAGE":
            content = "확인 가능한 제품 라벨이나 오류 표시창 사진을 첨부해 주실 수 있을까요?"
        elif action.action == "CHECK_SCHEDULE":
            content = "접수된 정보를 바탕으로 방문 가능한 시간을 확인해 주세요."
        elif action.action == "ESCALATE_TO_OWNER":
            content = "사장님 확인이 필요한 내용입니다. 화면의 상담 연결 절차로 진행해 주세요."
        elif action.action == "UNSERVICEABLE":
            content = "업체에서 제공하는 작업인지 확인이 필요합니다."
        elif action.action == "ANSWER_SIDE_QUESTION":
            hits = state.get("manualHits", [])
            if hits:
                content = (
                    "해당 제품의 매뉴얼에서 관련 내용을 확인했습니다. 참고 자료를 확인해 주세요."
                )
                interpretations = [
                    Interpretation(
                        kind="MANUAL",
                        text=hit.text,
                        reference={
                            "documentId": hit.documentId,
                            "page": str(hit.page) if hit.page is not None else "",
                        },
                    )
                    for hit in hits
                ]
                return {
                    "stage": "SUMMARY",
                    "reply": Reply(content=content),
                    "interpretations": interpretations,
                }
            content = "확인 가능한 제품별 매뉴얼 근거가 없어 단정하여 안내하기 어렵습니다."
        else:
            content = "현재 문의 내용을 확인했습니다."
        return {"stage": "SUMMARY", "reply": Reply(content=content), "interpretations": []}

    def validate_result(state: IntakeState) -> dict:
        req = state["request"]
        valid_messages = {m.messageId for m in req.messages}
        valid_photos = {p.photoId for p in req.photos}
        # 기존 DB 필드에 연결된 사진/메시지 근거도 이 요청의 조회 범위 안에서만 유효하다.
        valid_photos.update(f.photoId for f in req.fields if f.photoId is not None)
        valid_messages.update(f.messageId for f in req.fields if f.messageId is not None)
        for fact in state["facts"]:
            if (
                fact.evidence.messageId is not None
                and fact.evidence.messageId not in valid_messages
            ):
                raise ValueError("다른 문의의 메시지 근거를 반환할 수 없습니다")
            if fact.evidence.photoId is not None and fact.evidence.photoId not in valid_photos:
                raise ValueError("다른 문의의 사진 근거를 반환할 수 없습니다")
        for conflict in state["conflicts"]:
            for candidate in conflict.candidates:
                if (
                    candidate.evidence
                    and candidate.evidence.messageId is not None
                    and candidate.evidence.messageId not in valid_messages
                ):
                    raise ValueError("다른 문의의 메시지 근거를 반환할 수 없습니다")
                if (
                    candidate.evidence
                    and candidate.evidence.photoId is not None
                    and candidate.evidence.photoId not in valid_photos
                ):
                    raise ValueError("다른 문의의 사진 근거를 반환할 수 없습니다")
        if state["safety"].flag and state["nextAction"].action == "CHECK_SCHEDULE":
            raise ValueError("안전 미해결 시 자동 예약 연결 불가")
        # 10/26 확정 기준: 필수정보 또는 미분류 항목의 충돌만 예약 진입 차단.
        # 참고정보 충돌은 상태를 UNCONFIRMED로 전달하고 Spring에서 최종 검증한다.
        required = rules.required_fields(state["workCategory"].value)
        blocking_conflicts = [
            conflict.fieldKey
            for conflict in state["conflicts"]
            if conflict.fieldKey in required or conflict.fieldKey not in rules.referenceFields
        ]
        if blocking_conflicts and state["nextAction"].action == "CHECK_SCHEDULE":
            raise ValueError("필수정보 충돌 해결 전 자동 예약 연결 불가")
        # 단순 참고 항목 미확인으로 사장님 알림·이관을 생성하면 안 된다.
        output = AnalyzeResult(
            requestId=req.requestId,
            inquiryId=req.inquiryId,
            inputVersion=req.inputVersion,
            ruleVersion=rules.version,
            promptVersion=PROMPT_VERSION,
            decidedBy="RULE",
            modelName=getattr(provider, "model", "mock"),
            usage=Usage(
                latencyMs=0,
                inputTokens=getattr(provider, "input_tokens", 0),
                outputTokens=getattr(provider, "output_tokens", 0),
            ),
            facts=state["facts"],
            conflicts=state["conflicts"],
            interpretations=state["interpretations"],
            workCategory=state["workCategory"],
            safety=state["safety"],
            intent=state["intent"],
            intentEvidence=state.get("intentEvidence"),
            analyzedPhotoIds=state.get("analyzedPhotoIds", []),
            nextAction=state["nextAction"],
            reply=state["reply"],
        )
        return {"stage": "SUMMARY", "result": output}

    graph = StateGraph(IntakeState)

    # 1. 노드 등록
    for name, node in (
        ("extract_message", aextract_message if asynchronous else extract_message),
        ("analyze_image", aanalyze_image if asynchronous else analyze_image),
        ("merge_facts", merge_facts),
        ("check_rules", check_rules),
        ("decide_action", decide_action),
        ("search_manual", search_manual),
        ("generate_reply", generate_reply),
        ("validate_result", validate_result),
    ):
        graph.add_node(name, node)

    # 2. 시작
    graph.add_edge(START, "extract_message")

    # 3. 사진 유무에 따른 분기
    graph.add_conditional_edges(
        "extract_message",
        needs_image,
        ["analyze_image", "merge_facts"],
    )

    # 4. 사진 분석 후 정보 통합
    graph.add_edge("analyze_image", "merge_facts")
    graph.add_edge("merge_facts", "check_rules")

    # 5. 강제 처리 여부에 따른 분기
    graph.add_conditional_edges(
        "check_rules",
        choose_route,
        ["generate_reply", "decide_action"],
    )

    # 6. 매뉴얼 검색 필요 여부에 따른 분기
    graph.add_conditional_edges(
        "decide_action",
        needs_manual,
        ["search_manual", "generate_reply"],
    )

    # 7. 답변 생성 및 검증
    graph.add_edge("search_manual", "generate_reply")
    graph.add_edge("generate_reply", "validate_result")
    graph.add_edge("validate_result", END)

    return graph.compile()
