"""AI-02 비식별 고정 사례의 실제 Gemini 검증. Spring은 메모리로 모사한다.

실행: uv run python -X utf8 check_ai02_live.py --case all
실제 API 사용료가 발생한다. 제공사 오류 시 즉시 중단하며 자동 재시도하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.intake.contracts import ExistingField
from simulate_ilro_multiturn import SimulatedSpring, execute_turn

TurnRunner = Callable[[SimulatedSpring, str, str, str], dict[str, Any]]

CASES = {
    "negation": {
        "label": "부정 표현",
        "turns": ["온수는 잘 나오는데 난방이 안 돼요. 수리받고 싶어요."],
        "expected": "난방 문제 확인, 온수 고장으로 해석하지 않고 CHECK_SCHEDULE",
    },
    "correction": {
        "label": "고객 정정",
        "turns": [
            "난방이 안 돼요.",
            "제가 잘못 말했어요. 난방은 정상이고 온수가 안 나와요. 수리받고 싶어요.",
        ],
        "expected": "이전 난방 증상을 온수 증상으로 정정, 충돌·증상 재질문 없이 CHECK_SCHEDULE",
    },
    "owner": {
        "label": "사장님 확인값 보호",
        "turns": ["제가 잘못 알았어요. 귀뚜라미 보일러인데 온수가 안 나와요. 수리받고 싶어요."],
        "expected": "OWNER 제조사 경동나비엔 유지, 고객 귀뚜라미 설명은 충돌로 재확인",
    },
}


def has_problem(value: str | None, target: str) -> bool:
    """고정 사례에서 사용할 유한한 표현 검사. 일반적인 의미 판별기는 아니다."""
    compact = re.sub(r"\s+", "", value or "")
    prefix = rf"{target}(?:이|가|은|는|도|만|쪽|기능|작동)*"
    modifiers = r"(?:전혀|잘|제대로|정상적으로|계속|아예)*"
    problem = r"(?:안(?:돼|되|됩|나오|나와|켜|돌)|(?:되|나오|작동하)지않|불량|고장|불가|장애)"
    return bool(re.search(prefix + modifiers + problem, compact))


def check_case(case_name: str, traces: list[dict[str, Any]]) -> dict[str, bool]:
    """모델 후보·AI 결과·모사 저장값을 각각 확인해 값 보호만으로 통과시키지 않는다."""
    last = traces[-1]
    result = last["result"]
    saved = last["spring_after_ai_result"]["fields"]
    extracted = next(
        update["updates"]["observations"]
        for update in last["node_updates"]
        if update["node"] == "extract_message"
    )
    checks = {"매 턴 최신 결과 적용": all(t["apply_status"] == "APPLIED" for t in traces)}
    if case_name in {"negation", "correction"}:
        target, other = ("난방", "온수") if case_name == "negation" else ("온수", "난방")
        symptom = saved.get("symptom", {})
        checks.update(
            {
                "기대한 증상 확인": symptom.get("status") == "CONFIRMED"
                and has_problem(symptom.get("value"), target),
                "정상인 부분을 고장으로 기록하지 않음": not has_problem(
                    symptom.get("value"), other
                ),
                "증상 충돌 없음": not any(c["fieldKey"] == "symptom" for c in result["conflicts"]),
                "예약 단계 제안": result["nextAction"]["action"] == "CHECK_SCHEDULE",
            }
        )
        if case_name == "correction":
            first = traces[0]["spring_after_ai_result"]["fields"].get("symptom", {})
            latest_id = last["request_sent_to_ai"]["messages"][-1]["messageId"]
            checks.update(
                {
                    "정정 전 난방 증상 저장": first.get("status") == "CONFIRMED"
                    and has_problem(first.get("value"), "난방"),
                    "최신 고객 정정 후보 추출": any(
                        item.get("isCorrection")
                        and item["evidence"].get("messageId") == latest_id
                        and has_problem(item.get("value"), "온수")
                        for item in extracted
                        if item["fieldKey"] == "symptom"
                    ),
                    "새 고객 원문 근거 저장": symptom.get("messageId") == latest_id,
                }
            )
    else:
        manufacturer = saved.get("manufacturer", {})
        checks.update(
            {
                "고객의 다른 제조사도 추출": any(
                    item["fieldKey"] == "manufacturer" and item["value"] == "귀뚜라미"
                    for item in extracted
                ),
                "AI가 OWNER 값 변경을 제안하지 않음": not any(
                    fact["fieldKey"] == "manufacturer" for fact in result["facts"]
                ),
                "OWNER 현재값 유지": manufacturer.get("value") == "경동나비엔"
                and manufacturer.get("source") == "OWNER"
                and manufacturer.get("status") == "CONFIRMED",
                "제조사 충돌 표시": any(
                    conflict["fieldKey"] == "manufacturer" for conflict in result["conflicts"]
                ),
                "제조사 재확인": result["nextAction"]["action"] == "ASK_MORE_INFO"
                and result["nextAction"]["targetFieldKeys"] == ["manufacturer"],
            }
        )
    return checks


def run_case(
    case_name: str,
    model: str,
    *,
    turn_runner: TurnRunner = execute_turn,
) -> dict[str, Any]:
    case = CASES[case_name]
    db = SimulatedSpring()
    if case_name == "owner":
        db.fields["manufacturer"] = ExistingField(
            fieldKey="manufacturer", value="경동나비엔", source="OWNER", status="CONFIRMED"
        )
    report: dict[str, Any] = {
        "case": case_name,
        "expected": case["expected"],
        "traces": [],
    }
    print(f"\n========== {case['label']} ==========", flush=True)
    print("기대:", case["expected"], flush=True)
    for text in case["turns"]:
        print("고객:", text, flush=True)
        started = time.perf_counter()
        try:
            trace = turn_runner(db, text, "gemini", model)
        except Exception as exc:
            # SDK 예외 원문에는 키나 요청 내용이 들어갈 수 있으므로 분류만 기록한다.
            report.update(
                {
                    "status": "API_ERROR",
                    "errorType": type(exc).__name__,
                    "httpCode": getattr(exc, "code", None),
                    "elapsedMs": int((time.perf_counter() - started) * 1000),
                }
            )
            print("호출/실행 실패:", report["errorType"], report["httpCode"], flush=True)
            return report
        trace["elapsedMs"] = int((time.perf_counter() - started) * 1000)
        report["traces"].append(trace)
        print("다음 행동:", trace["result"]["nextAction"], flush=True)
        print(
            "저장값:",
            json.dumps(trace["spring_after_ai_result"]["fields"], ensure_ascii=False),
            flush=True,
        )
        print("토큰:", trace["result"]["usage"], "실험 경과(ms):", trace["elapsedMs"], flush=True)
    report["checks"] = check_case(case_name, report["traces"])
    report["status"] = "PASS" if all(report["checks"].values()) else "CHECK_FAILED"
    for name, passed in report["checks"].items():
        print("PASS" if passed else "FAIL/표현 검토 필요", name, flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-02 부정·정정·OWNER 값 보호 실호출")
    parser.add_argument("--case", choices=["all", *CASES], default="all")
    parser.add_argument("--model", default="gemini-3.5-flash-lite")
    parser.add_argument("--save", type=Path, help="비식별 실험 상세 JSON 저장 경로")
    args = parser.parse_args()
    print("실제 Gemini 호출·과금 발생. Spring/DB는 메모리 모사입니다.", flush=True)
    names = list(CASES) if args.case == "all" else [args.case]
    reports = []
    for name in names:
        report = run_case(name, args.model)
        reports.append(report)
        if report["status"] == "API_ERROR":
            print("호출 오류로 남은 사례를 중단합니다. 자동 재시도하지 않습니다.", flush=True)
            break
    summary = {
        "timestamp": datetime.now(UTC).isoformat(),
        "model": args.model,
        "scope": "실제 Gemini + Spring 메모리 모사; 고정 표현 검사는 수동 검토 필요",
        "requestedCases": names,
        "cases": reports,
        "allPassed": len(reports) == len(names) and all(r["status"] == "PASS" for r in reports),
    }
    print("\n최종:", "PASS" if summary["allPassed"] else "미통과/확인 필요", flush=True)
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print("실험 기록:", args.save, flush=True)
    if not summary["allPassed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
