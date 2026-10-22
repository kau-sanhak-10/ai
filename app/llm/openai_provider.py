"""Day 4: OpenAI 후보의 문자/사진/구조화 출력 인터페이스.

실제 호출은 OpenAI API 크레딧이 준비된 경우에만 가능하다.
무료 검증: uv run pytest tests/test_llm_openai.py
"""

import base64
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.llm.gemini_provider import PROMPT, read_image, verify_evidence
from app.llm.schemas import BoilerExtraction

BASE_DIR = Path(__file__).resolve().parents[2]
USAGE_DB_PATH = BASE_DIR / "results" / "openai_calls.sqlite3"


def make_content(message: str, image_path: Path | None = None) -> list[dict]:
    """고객 입력을 OpenAI Responses API 콘텐츠로 변환한다."""
    if not message.strip():
        raise ValueError("고객 메시지가 비어 있습니다.")
    content = [{"type": "input_text", "text": PROMPT + message}]
    if image_path is not None:
        data, mime_type = read_image(image_path)
        b64 = base64.b64encode(data).decode("ascii")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{mime_type};base64,{b64}",
            }
        )
    return content


def _usage_data(response: object) -> dict:
    """OpenAI 응답에서 요청별 입력·출력·총 토큰 수를 꺼낸다."""
    usage = getattr(response, "usage", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _error_details(error: Exception) -> tuple[str, str, str]:
    """예외에서 안전하게 저장할 상태 코드와 오류 분류만 추린다."""
    status_code = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    error_body = body.get("error", body) if isinstance(body, dict) else {}
    error_code = error_body.get("code") if isinstance(error_body, dict) else None
    error_code = str(error_code)[:100] if error_code is not None else ""

    if status_code == 429:
        if error_code in ("insufficient_quota", "credit_balance_exhausted"):
            category = "quota_exhausted"
        elif error_code == "rate_limit_exceeded":
            category = "rate_limited"
        else:
            category = "rate_or_quota"
    elif status_code in (401, 403):
        category = "access"
    elif status_code in (400, 404):
        category = "bad_request"
    elif isinstance(status_code, int) and status_code >= 500:
        category = "provider_server"
    elif type(error).__name__ in ("APITimeoutError", "APIConnectionError"):
        category = "transient"
    else:
        category = "other"
    return type(error).__name__, error_code, category


def _record_call(path: Path, record: dict) -> None:
    """키와 사용자 입력을 제외한 요청 메타데이터를 SQLite에 저장한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS openai_calls (
                request_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                has_image INTEGER NOT NULL,
                status TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                latency_ms INTEGER NOT NULL,
                error_type TEXT,
                error_code TEXT,
                error_category TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO openai_calls (
                request_id, timestamp, provider, model, has_image, status,
                input_tokens, output_tokens, total_tokens, latency_ms,
                error_type, error_code, error_category
            ) VALUES (
                :request_id, :timestamp, 'openai', :model, :has_image, :status,
                :input_tokens, :output_tokens, :total_tokens, :latency_ms,
                :error_type, :error_code, :error_category
            )
            """,
            record,
        )


def summarize_calls(path: Path = USAGE_DB_PATH) -> dict:
    """OpenAI 요청 로그의 성공/실패 및 토큰 합계와 안전한 요청 메타데이터를 반환한다."""
    if not path.is_file():
        return {"found": False, "calls": 0, "success": 0, "failed": 0, "tokens": {}}
    with sqlite3.connect(path, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT request_id, timestamp, model, has_image, status,
                   input_tokens, output_tokens, total_tokens, latency_ms,
                   error_type, error_code, error_category
            FROM openai_calls ORDER BY timestamp, request_id
            """
        ).fetchall()

    token_names = ("input_tokens", "output_tokens", "total_tokens")
    totals = {name: sum(row[name] or 0 for row in rows) for name in token_names}
    return {
        "found": True,
        "calls": len(rows),
        "success": sum(row["status"] == "success" for row in rows),
        "failed": sum(row["status"] == "failed" for row in rows),
        "tokens": totals,
        "requests": [dict(row) for row in rows],
    }


def call_openai_multimodal(
    api_key: str,
    model: str,
    message: str,
    image_path: Path | None = None,
    usage_db_path: Path = USAGE_DB_PATH,
) -> tuple[BoilerExtraction, dict]:
    """OpenAI에 멀티모달 요청을 보내고 응답을 검증한 뒤 안전 로그를 남긴다.

    실패도 기록한 뒤 예외를 다시 전달한다. SDK 자동 재시도는 비활성화되어 있다.
    """
    if not api_key.strip():
        raise ValueError("OPENAI_API_KEY가 비어 있습니다.")
    if not model.strip():
        raise ValueError("OPENAI_MODEL이 비어 있습니다.")
    content = make_content(message, image_path)
    request_id = uuid.uuid4().hex
    started = time.perf_counter()
    usage_data = {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }
    from openai import OpenAI

    try:
        with OpenAI(api_key=api_key, timeout=30.0, max_retries=0) as client:
            response = client.responses.parse(
                model=model,
                input=[{"role": "user", "content": content}],
                text_format=BoilerExtraction,
            )
        usage_data = _usage_data(response)
        extracted = response.output_parsed
        if extracted is None:
            raise ValueError("GPT 구조화 출력 결과가 없습니다(거부/불완전 응답 가능).")
        # 로컬 mock과 실제 SDK 모두 Pydantic 모델로 정규화한다.
        extracted = BoilerExtraction.model_validate(extracted)
        extracted = verify_evidence(extracted, message, image_path is not None)
    except Exception as error:
        error_type, error_code, error_category = _error_details(error)
        _record_call(
            usage_db_path,
            {
                "request_id": request_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "model": model,
                "has_image": image_path is not None,
                "status": "failed",
                **usage_data,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error_type": error_type,
                "error_code": error_code,
                "error_category": error_category,
            },
        )
        raise

    _record_call(
        usage_db_path,
        {
            "request_id": request_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "model": model,
            "has_image": image_path is not None,
            "status": "success",
            **usage_data,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "error_type": None,
            "error_code": None,
            "error_category": None,
        },
    )
    return extracted, {**usage_data, "request_id": request_id}


def call_openai_from_environment(
    model: str,
    message: str,
    image_path: Path | None = None,
    usage_db_path: Path = USAGE_DB_PATH,
) -> tuple[BoilerExtraction, dict]:
    """서버 프로세스 환경변수에서 비밀키를 읽어 호출한다."""
    from app.core.config import Settings

    secret = Settings(_env_file=None).openai_api_key
    api_key = secret.get_secret_value().strip() if secret is not None else ""
    if not api_key or api_key.startswith("paste_your_"):
        raise ValueError("서버 환경변수 OPENAI_API_KEY가 설정되지 않았습니다.")
    return call_openai_multimodal(api_key, model, message, image_path, usage_db_path)
