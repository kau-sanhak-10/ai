"""DS-04 6장: Spring → FastAPI 내부 문의 분석 엔드포인트."""

import asyncio
import hmac
import time

from fastapi import APIRouter, Header
from pydantic import ValidationError

from app.common.errors import BusinessException, ErrorCode
from app.common.response import ApiResponse
from app.core.config import get_settings
from app.intake.audit import record_analysis
from app.intake.context import PROMPT_VERSION
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


async def _analyze(payload: AnalyzeRequest) -> AnalyzeResult:
    settings = get_settings()
    if payload.handlerMode != "AI":
        raise BusinessException(ErrorCode.INVALID_INPUT)
    started = time.perf_counter()
    provider = None
    rules = None
    status = "OK"
    error_type = None
    try:
        try:
            rules = load_rules()
        except RuntimeError as exc:
            raise BusinessException(ErrorCode.AI_RULES_UNAVAILABLE) from exc
        secret = settings.openai_api_key
        if secret is None or not secret.get_secret_value().strip() or not settings.openai_model:
            raise BusinessException(ErrorCode.AI_MODEL_ERROR)
        provider = OpenAIExtractionProvider(
            secret.get_secret_value(),
            settings.openai_model,
            timeout_seconds=settings.intake_timeout_seconds,
        )
        graph = build_workflow(provider, rules, asynchronous=True)
        state = await graph.ainvoke({"request": payload, "observations": []})
        result: AnalyzeResult = state["result"]
        result.usage.latencyMs = int((time.perf_counter() - started) * 1000)
        return result
    except TimeoutError as exc:
        status, error_type = "AI_MODEL_TIMEOUT", type(exc).__name__
        raise BusinessException(ErrorCode.AI_MODEL_TIMEOUT) from exc
    except asyncio.CancelledError:
        # 외부 전체 제한이 취소한 분석도 finally에서 실패·사용량을 남긴다.
        status, error_type = "AI_MODEL_TIMEOUT", "CancelledError"
        raise
    except (ValueError, ValidationError, KeyError) as exc:
        status, error_type = "AI_OUTPUT_INVALID", type(exc).__name__
        raise BusinessException(ErrorCode.AI_OUTPUT_INVALID) from exc
    except BusinessException as exc:
        status, error_type = exc.error_code.code, type(exc).__name__
        raise
    except Exception as exc:
        # 외부 모델 SDK의 연결·권한·용량 문제는 내부 구현 정보를 노출하지 않는다.
        if type(exc).__name__ in ("APITimeoutError", "ReadTimeout", "ConnectTimeout"):
            status, error_type = "AI_MODEL_TIMEOUT", type(exc).__name__
            raise BusinessException(ErrorCode.AI_MODEL_TIMEOUT) from exc
        status, error_type = "AI_MODEL_ERROR", type(exc).__name__
        raise BusinessException(ErrorCode.AI_MODEL_ERROR) from exc
    finally:
        record_analysis(
            requestId=payload.requestId,
            inputVersion=payload.inputVersion,
            modelName=settings.openai_model,
            promptVersion=PROMPT_VERSION,
            ruleVersion=rules.version if rules else None,
            latencyMs=int((time.perf_counter() - started) * 1000),
            inputTokens=getattr(provider, "input_tokens", 0),
            outputTokens=getattr(provider, "output_tokens", 0),
            modelCalls=getattr(provider, "model_calls", 0),
            usageReportedCalls=getattr(provider, "usage_reported_calls", 0),
            status=status,
            errorType=error_type,
        )


@router.post("/inquiries/analyze", response_model=ApiResponse[AnalyzeResult])
async def analyze_inquiry(
    body: AnalyzeRequest,
    x_internal_key: str | None = Header(default=None, alias="X-Internal-Key"),
) -> ApiResponse[AnalyzeResult]:
    # JSON 본문·사진 원본·비밀키는 로그에 남기지 않는다.
    _authenticate(x_internal_key)
    try:
        async with asyncio.timeout(get_settings().intake_timeout_seconds):
            result = await _analyze(body)
    except TimeoutError as exc:
        raise BusinessException(ErrorCode.AI_MODEL_TIMEOUT) from exc
    return ApiResponse[AnalyzeResult].ok(result)
