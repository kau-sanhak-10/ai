"""DS-04 6장: Spring → FastAPI 내부 문의 분석 엔드포인트."""

import hmac
import time

from fastapi import APIRouter, Header
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError

from app.common.errors import BusinessException, ErrorCode
from app.common.response import ApiResponse
from app.core.config import get_settings
from app.intake.contracts import AnalyzeRequest, AnalyzeResult
from app.intake.graph import build_workflow
from app.intake.providers import OpenAIExtractionProvider
from app.intake.rules import load_rules

router = APIRouter(prefix="/internal/v1", tags=["internal-ai"])


def _authenticate(internal_key: str | None) -> None:
    configured = get_settings().internal_key
    if configured is None:
        raise BusinessException(ErrorCode.UNAUTHORIZED)
    expected = configured.get_secret_value()
    if not internal_key or not expected or not hmac.compare_digest(internal_key, expected):
        raise BusinessException(ErrorCode.UNAUTHORIZED)


def _analyze(payload: AnalyzeRequest) -> AnalyzeResult:
    settings = get_settings()
    if payload.handlerMode != "AI":
        raise BusinessException(ErrorCode.INVALID_INPUT)
    try:
        rules = load_rules()
    except RuntimeError as exc:
        raise BusinessException(ErrorCode.AI_RULES_UNAVAILABLE) from exc
    secret = settings.openai_api_key
    if secret is None or not secret.get_secret_value().strip() or not settings.openai_model:
        # 실제 모델 연결이 없다면 허위 결과를 반환하지 않는다. 테스트는 의존성 주입으로 진행.
        raise BusinessException(ErrorCode.AI_MODEL_ERROR)
    provider = OpenAIExtractionProvider(secret.get_secret_value(), settings.openai_model)
    started = time.perf_counter()
    try:
        graph = build_workflow(provider, rules)
        state = graph.invoke({"request": payload, "observations": []})
        result: AnalyzeResult = state["result"]
        result.usage.latencyMs = int((time.perf_counter() - started) * 1000)
        return result
    except TimeoutError as exc:
        raise BusinessException(ErrorCode.AI_MODEL_TIMEOUT) from exc
    except (ValueError, ValidationError, KeyError) as exc:
        raise BusinessException(ErrorCode.AI_OUTPUT_INVALID) from exc
    except BusinessException:
        raise
    except Exception as exc:
        # 외부 모델 SDK의 연결·권한·용량 문제는 내부 구현 정보를 노출하지 않는다.
        if type(exc).__name__ in ("APITimeoutError", "ReadTimeout", "ConnectTimeout"):
            raise BusinessException(ErrorCode.AI_MODEL_TIMEOUT) from exc
        raise BusinessException(ErrorCode.AI_MODEL_ERROR) from exc


@router.post("/inquiries/analyze", response_model=ApiResponse[AnalyzeResult])
async def analyze_inquiry(
    body: AnalyzeRequest,
    x_internal_key: str | None = Header(default=None, alias="X-Internal-Key"),
) -> ApiResponse[AnalyzeResult]:
    # JSON 본문·사진 원본·비밀키는 로그에 남기지 않는다.
    _authenticate(x_internal_key)
    result = await run_in_threadpool(_analyze, body)
    return ApiResponse[AnalyzeResult].ok(result)
