"""실제 Gemini 호출 없이 API 오류/사용량 감사 정책을 검증한다."""

import csv
from pathlib import Path

from app.llm.usage_audit import classify_error, summarize_csv


def test_access_errors_never_retry():
    """요청 형식 또는 인증 오류는 재시도하지 않는 정책인지 확인한다."""
    for code in (400, 401, 403, 404):
        assert classify_error(code)["retry"] is False


def test_rate_quota_requires_distinction():
    """429 응답은 속도 제한과 할당량을 추가 확인하도록 분류되는지 검사한다."""
    result = classify_error(429)
    assert result["retry"] == "conditional"
    assert result["category"] == "rate_or_quota"


def test_transient_error():
    """서버 오류와 타임아웃은 일시 오류로 분류되는지 확인한다."""
    assert classify_error(503)["category"] == "transient"
    assert classify_error(None, "APITimeoutError")["retry"] == "conditional"


def test_validation_failure_does_not_retry():
    """응답 데이터 검증 실패는 재시도하지 않는지 확인한다."""
    assert classify_error(None, "ValidationError")["retry"] is False


def test_missing_csv(tmp_path: Path):
    """로그 파일이 없을 때 호출이 0건인 결과를 반환하는지 검사한다."""
    data = summarize_csv(tmp_path / "missing.csv")
    assert data["found"] is False
    assert data["calls"] == 0


def test_csv_aggregate(tmp_path: Path):
    """성공·실패 행과 토큰 및 오류 코드가 정확히 집계되는지 확인한다."""
    path = tmp_path / "calls.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "status",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "error_type",
                "error_code",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "status": "success",
                "input_tokens": "100",
                "output_tokens": "20",
                "total_tokens": "125",
            }
        )
        writer.writerow({"status": "failed", "error_type": "ServerError", "error_code": "503"})
    data = summarize_csv(path)
    assert (data["calls"], data["success"], data["failed"]) == (2, 1, 1)
    assert data["tokens"]["total_tokens"] == 125
    assert data["errors"]["ServerError:503"] == 1
