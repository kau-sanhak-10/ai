"""Gemini 오류 분류, 로그 검사, 사용량 요약. 실제 API 호출 없음."""

import csv
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
CSV_PATH = BASE / "results" / "gemini_multimodal_calls.csv"


def classify_error(code: int | None, error_type: str = "") -> dict:
    """네트워크/HTTP 오류를 안전한 정책으로 분류한다 (실패 유도 없음)."""
    if code in (401, 403):
        return {
            "category": "access",
            "retry": False,
            "action": "API 키와 프로젝트/모델 접근 권한 확인",
        }
    if code == 400:
        return {
            "category": "bad_request",
            "retry": False,
            "action": "요청 형식과 이미지/스키마 확인",
        }
    if code == 404:
        return {"category": "not_found", "retry": False, "action": "모델 ID와 API 버전 확인"}
    if code == 429:
        return {
            "category": "rate_or_quota",
            "retry": "conditional",
            "action": "RPM/TPM과 일일 할당량 구분. 일시 제한만 대기 후 재시도",
        }
    if code in (408, 500, 502, 503, 504) or "Timeout" in error_type or "Connection" in error_type:
        return {
            "category": "transient",
            "retry": "conditional",
            "action": "SDK 기본 재시도 확인 후 제한된 대기/재시도",
        }
    if error_type in ("ValidationError", "JSONDecodeError"):
        return {
            "category": "invalid_output",
            "retry": False,
            "action": "출력 형식 검증 및 오류 기록",
        }
    return {"category": "unknown", "retry": False, "action": "개인정보 노출 없이 오류 종류 확인"}


def parse_number(row: dict, key: str) -> int | None:
    """CSV 값이 0 이상의 정수일 때만 반환하고 잘못된 값은 제외한다."""
    raw = (row.get(key) or "").strip()
    try:
        value = int(raw)
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


def summarize_csv(path: Path) -> dict:
    """Gemini 요청 CSV의 성공·실패, 토큰 합계, 오류 유형을 집계한다."""
    if not path.exists():
        return {"found": False, "path": str(path), "calls": 0, "success": 0, "failed": 0}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    statuses = Counter((r.get("status") or "unknown").strip() for r in rows)
    sums = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    token_rows = 0
    for row in rows:
        values = {key: parse_number(row, key) for key in sums}
        if values["total_tokens"] is not None:
            token_rows += 1
        for key, value in values.items():
            if value is not None:
                sums[key] += value
    errors = Counter(
        f"{r.get('error_type', 'Unknown')}:{r.get('error_code', '')}"
        for r in rows
        if (r.get("status") or "").strip() == "failed"
    )
    return {
        "found": True,
        "path": str(path),
        "calls": len(rows),
        "success": statuses["success"],
        "failed": statuses["failed"],
        "token_rows": token_rows,
        "tokens": sums,
        "errors": dict(errors),
        "note": (
            "CSV에 기록된 호출만 집계. "
            "실패/재시도 요청의 실제 청구 및 "
            "제공사 전체 사용량은 콘솔에서 별도 확인"
        ),
    }


def demo_errors() -> list[dict]:
    """대표 오류별 분류 정책을 외부 API 호출 없이 예시 결과로 만든다."""
    cases = [
        (400, ""),
        (401, ""),
        (403, ""),
        (404, ""),
        (429, ""),
        (503, ""),
        (None, "APITimeoutError"),
        (None, "ValidationError"),
    ]
    return [{"code": code, "exception": kind, **classify_error(code, kind)} for code, kind in cases]
