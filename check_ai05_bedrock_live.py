"""AI-05 실제 Bedrock 검증. 합성 문자만 전송하며 Spring·Dev에는 접속하지 않는다.

각 사례는 새 Python 프로세스의 실제 FastAPI/SDK 경로를 사용한다.
최대 8회의 정상 모델 호출과 1회의 잘못된 모델 ID 오류 요청을 실행한다. 자동 재시도는 없다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import subprocess
import sys
import traceback
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.intake.context import PROMPT_VERSION
from app.intake.contracts import AnalyzeResult
from app.intake.rules import load_rules

ROOT = Path(__file__).resolve().parent


def make_cases() -> list[dict[str, Any]]:
    request = {
        "requestId": "ai05-live-normal",
        "inquiryId": 905001,
        "ownerId": 905,
        "inputVersion": 1,
        "handlerMode": "AI",
        "business": {"activeWorkCategories": ["REPAIR"], "autoBookingEnabled": True},
        "fields": [],
        "askCounts": {},
        "messages": [
            {
                "messageId": 900,
                "sender": "CUSTOMER",
                "content": "보일러 온수가 안 나와서 수리를 받고 싶어요.",
                "createdAt": "2026-11-19T09:00:00+09:00",
            }
        ],
        "photos": [],
        "safetyFlag": False,
    }
    cases = []

    def add(name, action, *, status=200, calls=1, **changes):
        body = deepcopy(request)
        body["requestId"] = "ai05-live-" + name
        body.update(changes)
        case = {"name": name, "request": body, "action": action, "status": status, "calls": calls}
        cases.append(case)
        return case

    add("normal", "CHECK_SCHEDULE")
    add(
        "unprovided",
        "UNSERVICEABLE",
        business={"activeWorkCategories": ["INSPECTION"], "autoBookingEnabled": True},
    )
    add(
        "auto_disabled",
        "ESCALATE_TO_OWNER",
        business={"activeWorkCategories": ["REPAIR"], "autoBookingEnabled": False},
    )
    add(
        "reference_unknown",
        "CHECK_SCHEDULE",
        fields=[
            {
                "fieldKey": "model",
                "value": None,
                "source": "CUSTOMER",
                "status": "UNCONFIRMED",
                "unconfirmedReason": "CUSTOMER_UNSURE",
                "messageId": 900,
            }
        ],
        askCounts={"model": 2},
    )
    unknown = deepcopy(request["messages"])
    unknown[0]["content"] = "어떤 부분이 고장인지 잘 모르겠어요."
    add(
        "required_unresolved",
        "ESCALATE_TO_OWNER",
        messages=unknown,
        askCounts={"symptom": 2},
    )
    owner = add(
        "owner_resume",
        "CHECK_SCHEDULE",
        fields=[
            {
                "fieldKey": "symptom",
                "value": "사장님 확인 완료",
                "source": "OWNER",
                "status": "CONFIRMED",
                "messageId": 900,
            }
        ],
        messages=[{**unknown[0], "content": "확인해 주신 내용으로 수리를 진행해 주세요."}],
        askCounts={"symptom": 2},
    )
    # 동일 스냅샷을 새 AI 프로세스에 다시 전달한다. 실제 Spring 중복 방지는 검사하지 않는다.
    cases.append({**deepcopy(owner), "name": "owner_restart"})
    add(
        "safety_negated",
        "CHECK_SCHEDULE",
        messages=[
            {
                **unknown[0],
                "content": "가스 냄새는 전혀 없고 보일러 온수가 안 나와요. 수리받고 싶어요.",
            }
        ],
    )
    add("saved_safety", "ESCALATE_TO_OWNER", calls=0, safetyFlag=True)
    add(
        "gas_report",
        "ESCALATE_TO_OWNER",
        calls=0,
        messages=[{**unknown[0], "content": "지금 보일러에서 가스 냄새가 나요."}],
    )
    add(
        "direct_contact",
        "ESCALATE_TO_OWNER",
        calls=0,
        messages=[{**unknown[0], "content": "사장님과 직접 이야기하고 싶어요. 연결해주세요."}],
    )
    add("owner_mode", None, status=400, calls=0, handlerMode="OWNER")
    add("invalid_model", None, status=502)["invalidModel"] = True
    return cases


def check_response(case: dict, response: dict, model: str) -> dict[str, bool]:
    body = response["body"]
    events = response["audit"]
    checks = {
        "http_status": response["httpStatus"] == case["status"],
        "single_attempt": sum(e.get("modelCalls", 0) for e in events) == case["calls"],
    }
    if case["status"] != 200:
        checks["error_code"] = body.get("code") == (
            "INVALID_INPUT" if case["status"] == 400 else "AI_MODEL_ERROR"
        )
        checks["no_partial_result"] = body.get("result") is None
        return checks
    try:
        result = AnalyzeResult.model_validate(body.get("result"))
    except ValueError:
        return {**checks, "result_schema": False}
    checks.update(
        {
            "result_schema": True,
            "success": body.get("success") is True and body.get("code") == "OK",
            "action": result.nextAction.action == case["action"],
            "request_identity": all(
                getattr(result, key) == case["request"][key]
                for key in ("requestId", "inquiryId", "inputVersion")
            ),
            "versions": result.ruleVersion == load_rules().version
            and result.promptVersion == PROMPT_VERSION,
            "model": result.modelName == model,
            "no_owner_fact": all(f.source != "OWNER" for f in result.facts),
            "no_photo_result": result.analyzedPhotoIds == [],
        }
    )
    if case["calls"]:
        checks["real_usage"] = result.usage.inputTokens > 0 and result.usage.outputTokens > 0
    if case["name"] == "normal":
        checks["grounded_symptom"] = any(
            f.fieldKey == "symptom" and f.status == "CONFIRMED" and f.evidence.messageId == 900
            for f in result.facts
        )
    if case["name"] in {"owner_resume", "owner_restart"}:
        checks["owner_value_preserved"] = not any(f.fieldKey == "symptom" for f in result.facts)
        checks["no_requestion"] = result.nextAction.targetFieldKeys == []
    if case["name"] == "required_unresolved":
        checks["required_target"] = result.nextAction.targetFieldKeys == ["symptom"]
    if case["name"] in {"safety_negated", "saved_safety", "gas_report"}:
        checks["safety_flag"] = result.safety.flag == (case["name"] != "safety_negated")
    return checks


class AuditCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events: list[dict] = []

    def emit(self, record):
        self.events.append(json.loads(record.getMessage()))


def worker() -> None:
    task = json.load(sys.stdin)
    # AWS_PROFILE/SDK 설정은 유지하고 앱 설정만 격리한다. 실제 .env·내부 키를 변경하지 않는다.
    Settings.model_config["env_file"] = None
    for name in Settings.model_fields:
        os.environ.pop(name.upper(), None)
    key = secrets.token_hex(24)
    os.environ.update(INTERNAL_KEY=key, BEDROCK_MODEL_ID=task["model"])
    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.api import inquiries
    from app.main import create_app

    outputs = []
    original_provider = inquiries.BedrockExtractionProvider

    class ObservedProvider(original_provider):
        def _message_output(self, output, messages):
            # 실제 SDK 결과를 그대로 처리하고 합성 입력의 구조화 후보만 별도로 보존한다.
            outputs.append(output.model_dump(mode="json"))
            return super()._message_output(output, messages)

    inquiries.BedrockExtractionProvider = ObservedProvider
    audit = AuditCapture()
    logger = logging.getLogger("app.intake.audit")
    logger.addHandler(audit)
    try:
        with TestClient(create_app()) as api:
            result = api.post(
                "/internal/v1/inquiries/analyze",
                json=task["request"],
                headers={"X-Internal-Key": key},
            )
    finally:
        logger.removeHandler(audit)
        inquiries.BedrockExtractionProvider = original_provider
    json.dump(
        {
            "pid": os.getpid(),
            "httpStatus": result.status_code,
            "body": result.json(),
            "audit": audit.events,
            "modelOutputs": outputs,
        },
        sys.stdout,
        ensure_ascii=True,
    )


def run_process(case: dict, model: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--worker"],
        input=json.dumps({"request": case["request"], "model": model}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        timeout=40,
        check=True,
    )
    return json.loads(completed.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--profile")
    parser.add_argument("--expected-account")
    parser.add_argument("--model")
    parser.add_argument("--case", action="append", choices=[c["name"] for c in make_cases()])
    parser.add_argument("--save", type=Path)
    args = parser.parse_args()
    if args.worker:
        worker()
        return 0
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    model = args.model or get_settings().bedrock_model_id
    if not model:
        parser.error("BEDROCK_MODEL_ID 또는 --model이 필요합니다")
    selected = set(args.case or [c["name"] for c in make_cases()])
    if "owner_restart" in selected:
        selected.add("owner_resume")
    cases = [c for c in make_cases() if c["name"] in selected]
    report: dict[str, Any] = {
        "startedAt": datetime.now(UTC).isoformat(),
        "model": model,
        "scope": "local FastAPI ASGI + real Bedrock; no Spring/Dev/DB",
        "sourceHashes": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "app/intake/graph.py",
                "app/intake/contracts.py",
                "app/intake/context.py",
                "app/intake/providers.py",
                "app/intake/bedrock_provider.py",
                "app/api/inquiries.py",
                "rules/inquiry_types.yaml",
            )
        },
        "cases": [],
        "selectedCases": [c["name"] for c in cases],
    }
    try:
        import boto3
        from botocore.config import Config

        session = boto3.Session()
        with closing(
            session.client(
                "sts",
                config=Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1}),
            )
        ) as sts:
            identity = sts.get_caller_identity()
        if args.expected_account and identity["Account"] != args.expected_account:
            raise ValueError("AWS_ACCOUNT_MISMATCH")
        report["aws"] = {"account": identity["Account"], "region": session.region_name}
        for case in cases:
            actual_model = (
                "ai05-deliberately-invalid-model-id" if case.get("invalidModel") else model
            )
            response = run_process(case, actual_model)
            checks = check_response(case, response, actual_model)
            if case["name"] == "owner_restart":
                previous = next(c for c in report["cases"] if c["name"] == "owner_resume")
                checks["new_process"] = response["pid"] != previous["response"]["pid"]
                checks["same_snapshot"] = case["request"] == previous["request"]
            row = {**case, "response": response, "checks": checks, "passed": all(checks.values())}
            report["cases"].append(row)
            usage = (response["body"].get("result") or {}).get("usage", {})
            print(
                f"{case['name']}: {'PASS' if row['passed'] else 'FAIL'} "
                f"HTTP={response['httpStatus']} usage={usage}",
                flush=True,
            )
            if not row["passed"]:
                print("실패 항목:", [key for key, value in checks.items() if not value], flush=True)
            # 실제 호출 실패는 추가 비용·권한 오류 반복을 막기 위해 이후 사례도 중단한다.
            if response["httpStatus"] >= 500 and case["status"] == 200:
                report["stoppedOnApiError"] = True
                break
    except Exception as exc:
        # 예외 원문·프로파일 내용·자격 증명을 출력하거나 저장하지 않는다.
        report["errorType"] = type(exc).__name__
        report["awsErrorCode"] = getattr(exc, "response", {}).get("Error", {}).get("Code")
        report["errorLocation"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)
        ]
        print("검증 중단:", report["errorType"], report["awsErrorCode"], flush=True)
        print("오류 위치:", report["errorLocation"], flush=True)
    report["completedAt"] = datetime.now(UTC).isoformat()
    report["passed"] = len(report["cases"]) == len(cases) and all(
        c["passed"] for c in report["cases"]
    )
    report["totals"] = {
        key: sum(e.get(key, 0) for c in report["cases"] for e in c["response"]["audit"])
        for key in ("modelCalls", "inputTokens", "outputTokens", "latencyMs")
    }
    target = args.save or (
        ROOT / "results" / f"ai05_bedrock_live_{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS" if report["passed"] else "INCOMPLETE/FAIL", report["totals"], flush=True)
    print("저장:", target, flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
