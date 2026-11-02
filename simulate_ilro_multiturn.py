"""일로 DS-05: 같은 inquiryId에서 Spring 상태를 매 턴 재전송하는 로컬 통합 시뮬레이터.

프로젝트 ai 루트에 넣고 실행:
    uv run python simulate_ilro_multiturn.py --mode mock --save multiturn_trace.json
    uv run python simulate_ilro_multiturn.py --mode gemini --save multiturn_gemini.json
    uv run python simulate_ilro_multiturn.py --mode gemini --interactive

주의: 실제 Spring/DB/예약 API에는 접속하지 않는다. Python 메모리로 Spring을 모사한다.
Mock 모델은 미리 정해진 '최신 발화' 추출값을 반환하며 자연어를 이해하지 않는다.
Gemini 모드는 기존 try_my_intake_gemini.py를 사용하고 외부 API 요금이 발생할 수 있다.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic_core import to_jsonable_python

from app.intake.contracts import (
    AnalyzeRequest,
    AnalyzeResult,
    CategoryDecision,
    Evidence,
    ExistingField,
    Message,
    RawObservation,
    Safety,
    Understanding,
)
from app.intake.graph import build_workflow
from app.intake.rules import load_rules

DEFAULT_TURNS = [
    "보일러가 고장 났어요.",
    "온수가 안 나와요.",
    "수리받고 싶어요.",
]


class ThreeTurnMockProvider:
    """상태 전달 실험 전용: 현재 고객 발화에 대응하는 가상의 추출 결과만 돌려준다."""

    model = "multiturn-scripted-mock"
    input_tokens = 0
    output_tokens = 0

    def extract_message(self, messages: list[Message]) -> Understanding:
        latest = messages[-1]
        if latest.content not in DEFAULT_TURNS:
            raise ValueError("Mock은 내장 3개 예문만 지원합니다. 자유 입력은 --mode gemini 사용")

        observations: list[RawObservation] = []
        category = None
        if latest.content == DEFAULT_TURNS[0]:
            observations.append(
                RawObservation(
                    fieldKey="symptom",
                    value="보일러가 고장 났어요",
                    source="CUSTOMER",
                    evidence=Evidence(messageId=latest.messageId),
                    observedText="보일러가 고장 났어요",
                )
            )
        elif latest.content == DEFAULT_TURNS[1]:
            observations.append(
                RawObservation(
                    fieldKey="symptom",
                    value="온수가 안 나와요",
                    source="CUSTOMER",
                    evidence=Evidence(messageId=latest.messageId),
                    observedText="온수가 안 나와요",
                )
            )
        elif latest.content == DEFAULT_TURNS[2]:
            category = "REPAIR"

        return Understanding(
            observations=observations,
            intent="ANSWER",
            workCategory=CategoryDecision(
                value=category,
                status="CONFIRMED" if category else "UNCONFIRMED",
            ),
            safety=Safety(flag=False, reasons=[]),
        )

    def analyze_image(self, photo: Any) -> Understanding:
        raise AssertionError("이 시나리오는 문자-only이므로 analyze_image를 실행하면 안 됩니다")


@dataclass
class SimulatedSpring:
    """실제 저장소가 아닌 Spring의 단일 문의 스냅샷·결과 적용 규칙을 재현한다."""

    inquiry_id: int = 101
    owner_id: int = 7
    input_version: int = 0
    handler_mode: str = "AI"
    fields: dict[str, ExistingField] = field(default_factory=dict)
    ask_counts: dict[str, int] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    safety_flag: bool = False
    applied_request_ids: set[str] = field(default_factory=set)
    next_message_id: int = 900
    # 요구사항 미확인 필드를 'CONFIRMED'로 너무 일찍 DB에 적용하지 않는다는 시뮬레이터 가정.
    # Spring의 실제 적용 정책은 팀과 합의해야 한다. False로 바꾸면 이 가정 없이 실험 가능.
    defer_asked_field: bool = True

    def add_customer_message(self, content: str) -> Message:
        self.next_message_id += 1
        message = Message(
            messageId=self.next_message_id,
            sender="CUSTOMER",
            content=content,
            createdAt=datetime.now(UTC).isoformat(),
        )
        self.messages.append(message)
        # 고객 신규 입력이 추가된 경우에만 ai_input_seq 증가
        self.input_version += 1
        return message

    def make_request(self) -> AnalyzeRequest:
        return AnalyzeRequest.model_validate(
            {
                "requestId": f"mt-{self.inquiry_id}-v{self.input_version}",
                "inquiryId": self.inquiry_id,
                "ownerId": self.owner_id,
                "inputVersion": self.input_version,
                "handlerMode": self.handler_mode,
                "safetyFlag": self.safety_flag,
                "business": {
                    "activeWorkCategories": ["INSPECTION", "REPAIR", "LEAK", "REPLACEMENT"],
                    "autoBookingEnabled": True,
                },
                "fields": [f.model_dump() for f in self.fields.values()],
                "askCounts": dict(self.ask_counts),
                "messages": [m.model_dump() for m in self.messages],
                "photos": [],
            }
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "inquiryId": self.inquiry_id,
            "inputVersion": self.input_version,
            "handlerMode": self.handler_mode,
            "fields": {k: to_jsonable_python(v) for k, v in self.fields.items()},
            "askCounts": dict(self.ask_counts),
            "messages": [to_jsonable_python(m) for m in self.messages],
            "safetyFlag": self.safety_flag,
        }

    def apply_result(self, result: AnalyzeResult) -> str:
        """스키마·문의/입력 버전·중복을 검증한 뒤 결과를 원자적 적용처럼 반영한다.

        이 메모리 구현은 실제 DB 트랜잭션/잠금/백엔드 구현 검증을 대신하지 않는다.
        """
        if result.inquiryId != self.inquiry_id:
            return "REJECT_WRONG_INQUIRY"
        if result.inputVersion != self.input_version or self.handler_mode != "AI":
            return "STALE"
        if result.requestId in self.applied_request_ids:
            return "DUPLICATE"

        # 기존 사장님 확정값을 AI가 변경하지 않는다.
        # 질문 대상은 아직 해소되지 않았으므로 모의 DB에서는 확정 적용을 잠시 보류한다.
        asked_fields = (
            set(result.nextAction.targetFieldKeys)
            if result.nextAction.action == "ASK_MORE_INFO"
            else set()
        )
        conflict_keys = {item.fieldKey for item in result.conflicts}
        for fact in result.facts:
            existing = self.fields.get(fact.fieldKey)
            if existing is not None and existing.source == "OWNER":
                continue
            if fact.fieldKey in conflict_keys and fact.status == "CONFIRMED":
                continue
            if (
                self.defer_asked_field
                and fact.fieldKey in asked_fields
                and fact.status == "CONFIRMED"
            ):
                continue
            self.fields[fact.fieldKey] = ExistingField(
                fieldKey=fact.fieldKey,
                value=fact.value,
                source=fact.source,
                status=fact.status,
                unconfirmedReason=fact.unconfirmedReason,
                messageId=fact.evidence.messageId,
                photoId=fact.evidence.photoId,
            )

        self.safety_flag = self.safety_flag or result.safety.flag
        self.applied_request_ids.add(result.requestId)

        # AI 답변 저장은 고객 입력이 아니므로 inputVersion을 증가시키지 않는다.
        self.next_message_id += 1
        self.messages.append(
            Message(
                messageId=self.next_message_id,
                sender="AI",
                content=result.reply.content,
                createdAt=datetime.now(UTC).isoformat(),
            )
        )
        if result.nextAction.action == "ASK_MORE_INFO":
            for key in result.nextAction.targetFieldKeys:
                self.ask_counts[key] = self.ask_counts.get(key, 0) + 1
        return "APPLIED"


def build_provider(mode: str, model: str):
    if mode == "mock":
        return ThreeTurnMockProvider()
    # 기존 파일의 Gemini 어댑터를 사용한다.
    from app.core.config import get_settings
    from try_my_intake_gemini import GeminiGraphTestProvider

    key = get_settings().gemini_api_key
    if key is None or not key.get_secret_value().strip():
        raise RuntimeError(".env에 GEMINI_API_KEY를 설정하세요")
    return GeminiGraphTestProvider(key.get_secret_value(), model)


def execute_turn(db: SimulatedSpring, text: str, mode: str, model: str) -> dict[str, Any]:
    before = db.snapshot()
    db.add_customer_message(text)
    request = db.make_request()
    provider = build_provider(mode, model)
    graph = build_workflow(provider, load_rules())  # 매 턴 새 그래프, 이전 checkpoint 없음

    updates = []
    states = []
    for event in graph.stream(
        {"request": request, "observations": []},
        stream_mode=["updates", "values"],
        version="v2",
    ):
        if event["type"] == "updates":
            for node, values in event["data"].items():
                updates.append({"node": node, "updates": to_jsonable_python(values)})
        elif event["type"] == "values":
            states.append(to_jsonable_python(event["data"]))

    if not states or len(states) != len(updates) + 1:
        raise RuntimeError("노드별 State 추적 개수가 일치하지 않습니다")
    final = states[-1]["result"]
    result = AnalyzeResult.model_validate(final)
    applied = db.apply_result(result)
    return {
        "customer": text,
        "spring_before_customer_input": before,
        "request_sent_to_ai": to_jsonable_python(request),
        "node_updates": updates,
        "result": final,
        "apply_status": applied,
        "spring_after_ai_result": db.snapshot(),
    }


def run_simulation(
    mode: str = "mock",
    *,
    model: str = "gemini-3.5-flash-lite",
    turns: Iterable[str] | None = None,
    defer_asked_field: bool = True,
    check_stale: bool = True,
) -> dict[str, Any]:
    if mode not in {"mock", "gemini"}:
        raise ValueError("mode는 mock 또는 gemini여야 합니다")
    messages = turns if turns is not None else DEFAULT_TURNS
    if mode == "mock" and messages != DEFAULT_TURNS:
        raise ValueError("Mock은 고정 3턴 시나리오 전용입니다. 자유 입력은 --mode gemini")

    db = SimulatedSpring(defer_asked_field=defer_asked_field)
    traces = []
    checks = []
    first_result: AnalyzeResult | None = None
    for index, text in enumerate(messages, start=1):
        if not text.strip():
            raise ValueError("고객 발화는 빈 문자열일 수 없습니다")
        print(f"\n========== {index}턴 ==========")
        print("고객:", text)
        trace = execute_turn(db, text, mode, model)
        traces.append(trace)
        print("요청 inputVersion:", trace["request_sent_to_ai"]["inputVersion"])
        print("실행 Node:", " → ".join(s["node"] for s in trace["node_updates"]))
        print("다음 행동:", trace["result"]["nextAction"]["action"])
        print("대상 필드:", trace["result"]["nextAction"]["targetFieldKeys"])
        print("AI 답변:", trace["result"]["reply"]["content"])
        print("Spring 적용:", trace["apply_status"])
        print("저장 fields:", json.dumps(db.snapshot()["fields"], ensure_ascii=False))
        print("저장 askCounts:", db.ask_counts)
        print("대화 메시지 개수:", len(db.messages), "(고객과 AI 모두 포함)")

        checks.append(
            {
                "turn": index,
                "version_matches": trace["result"]["inputVersion"] == db.input_version,
                "same_inquiry": trace["result"]["inquiryId"] == db.inquiry_id,
                "applied": trace["apply_status"] == "APPLIED",
            }
        )
        if index == 1:
            first_result = AnalyzeResult.model_validate(trace["result"])
        elif index == 2 and check_stale and first_result is not None:
            # 버전이 달라진 상태에 뒤늦게 도착한 1턴 결과가 DB를 덮어쓰면 안 된다.
            snapshot_before = db.snapshot()
            stale_status = db.apply_result(first_result)
            unchanged = db.snapshot() == snapshot_before
            checks.append({"test": "stale_result", "status": stale_status, "unchanged": unchanged})
            print("[지연된 1턴 응답 재적용]", stale_status, "| DB 불변:", unchanged)

        action = trace["result"]["nextAction"]["action"]
        if action in {"CHECK_SCHEDULE", "ESCALATE_TO_OWNER", "COMPLETE", "UNSERVICEABLE"}:
            if action == "CHECK_SCHEDULE":
                print("정보 수집 완료: 이후 방문 시간 선택·예약 확정은 Spring/프론트 단계입니다.")
            else:
                print(f"정보 수집 종료: {action} 후속 처리 단계로 넘어갑니다.")
            break

    if mode == "mock" and messages == DEFAULT_TURNS:
        expected = [
            ("ASK_MORE_INFO", ["symptom"]),
            ("ASK_MORE_INFO", ["work_category"]),
            ("CHECK_SCHEDULE", []),
        ]
        for i, (action, targets) in enumerate(expected):
            actual = traces[i]["result"]["nextAction"]
            checks.append(
                {
                    "turn": i + 1,
                    "scenario_expected": action,
                    "action_matches": actual["action"] == action,
                    "target_matches": actual["targetFieldKeys"] == targets,
                }
            )

    print("\n========== 최종 검증 ==========")
    ok = True
    for item in checks:
        valid = all(
            v is True
            for k, v in item.items()
            if k
            in {
                "version_matches",
                "same_inquiry",
                "applied",
                "unchanged",
                "action_matches",
                "target_matches",
            }
        )
        if item.get("test") == "stale_result":
            valid = valid and item.get("status") == "STALE"
        ok &= valid
        print("PASS" if valid else "FAIL", json.dumps(item, ensure_ascii=False))
    print("최종:", "PASS" if ok else "FAIL (로그를 확인해 주세요)")
    return {
        "simulation": "Spring 메모리 모사; 실제 DB/예약 API를 호출하지 않음",
        "mode": mode,
        "defer_asked_field": defer_asked_field,
        "traces": traces,
        "checks": checks,
        "all_passed": ok,
    }


def _interactive_turns() -> Iterator[str]:
    """이전 AI 답변을 본 다음 다음 고객 발화를 입력한다."""
    print("정보 수집이 끝날 때까지 최대 5턴 대화합니다. 빈 줄을 누르면 종료합니다.")
    for index in range(1, 6):
        answer = input(f"\n{index}턴 고객 메시지: ").strip()
        if not answer:
            break
        yield answer


def main() -> None:
    parser = argparse.ArgumentParser(description="일로 LangGraph 멀티턴 Spring State 전달 테스트")
    parser.add_argument("--mode", choices=("mock", "gemini"), default="mock")
    parser.add_argument("--model", default="gemini-3.5-flash-lite")
    parser.add_argument("--turn", action="append", help="Gemini 전용: 반복 지정한 고객 발화")
    parser.add_argument("--interactive", action="store_true", help="Gemini 전용: 직접 발화 입력")
    parser.add_argument("--save", type=Path, help="상세 State 기록을 JSON으로 저장")
    parser.add_argument(
        "--persist-asked-field",
        action="store_true",
        help="실험용: 추가질문 대상도 즉시 CONFIRMED로 저장하여 충돌 위험 재현",
    )
    args = parser.parse_args()
    if args.mode == "mock" and (args.turn or args.interactive):
        parser.error("Mock에서는 예문 3개가 고정입니다. 자유 입력은 --mode gemini")
    if args.turn and args.interactive:
        parser.error("--turn과 --interactive를 동시에 사용할 수 없습니다")
    if args.mode == "gemini":
        print("[안내] Gemini API에 고객 발화 전체가 매 턴 전송되며 과금될 수 있습니다")
    texts = args.turn
    if args.interactive:
        texts = _interactive_turns()
    report = run_simulation(
        mode=args.mode,
        model=args.model,
        turns=texts,
        defer_asked_field=not args.persist_asked_field,
    )
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n상세 로그 저장:", args.save.resolve())
    if not report["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
