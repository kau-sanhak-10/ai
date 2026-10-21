import logging
from enum import Enum
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.common.response import ApiResponse, FieldErrorResponse

log = logging.getLogger(__name__)


class ErrorCode(Enum):
    """백엔드 CommonErrorCode와 같은 코드·메시지. 도메인 오류는 아래에 이어서 추가한다."""

    INVALID_INPUT = (HTTPStatus.BAD_REQUEST, "잘못된 요청입니다")
    RESOURCE_NOT_FOUND = (HTTPStatus.NOT_FOUND, "요청한 리소스를 찾을 수 없습니다")
    METHOD_NOT_ALLOWED = (HTTPStatus.METHOD_NOT_ALLOWED, "지원하지 않는 HTTP 메서드입니다")
    INTERNAL_SERVER_ERROR = (HTTPStatus.INTERNAL_SERVER_ERROR, "서버 내부 오류가 발생했습니다")

    def __init__(self, status: HTTPStatus, message: str) -> None:
        self.status = status
        self.message = message

    @property
    def code(self) -> str:
        return self.name


class BusinessException(Exception):
    """우리가 직접 던지는 예외. 응답 코드는 ErrorCode가 정한다."""

    def __init__(self, error_code: ErrorCode) -> None:
        super().__init__(error_code.code)
        self.error_code = error_code


def failure(status: int, code: str, message: str, result: object = None) -> JSONResponse:
    body = ApiResponse(success=False, code=code, message=message, result=result)
    return JSONResponse(status_code=status, content=body.model_dump())


def from_error_code(error_code: ErrorCode, result: object = None) -> JSONResponse:
    return failure(error_code.status, error_code.code, error_code.message, result)


def _field_name(loc: tuple) -> str:
    # ("body", "items", 0, "name") -> "items.0.name"
    parts = loc[1:] if loc and loc[0] in ("body", "query", "path", "header", "cookie") else loc
    return ".".join(str(p) for p in parts)


async def handle_business(request: Request, exc: BusinessException) -> JSONResponse:
    log.warning("BusinessException: %s", exc.error_code.code)
    return from_error_code(exc.error_code)


async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    # JSON 형식 자체가 깨진 요청은 백엔드처럼 필드 목록 없이 응답한다
    if any(e.get("type") == "json_invalid" for e in errors):
        return from_error_code(ErrorCode.INVALID_INPUT)
    fields = [
        FieldErrorResponse(field=_field_name(tuple(e["loc"])), reason=e["msg"]).model_dump()
        for e in errors
    ]
    return from_error_code(ErrorCode.INVALID_INPUT, fields)


async def handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == HTTPStatus.NOT_FOUND:
        return from_error_code(ErrorCode.RESOURCE_NOT_FOUND)
    if exc.status_code == HTTPStatus.METHOD_NOT_ALLOWED:
        return from_error_code(ErrorCode.METHOD_NOT_ALLOWED)
    status = HTTPStatus(exc.status_code)
    return failure(status, status.name, str(exc.detail))


async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled exception")
    return from_error_code(ErrorCode.INTERNAL_SERVER_ERROR)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(BusinessException, handle_business)
    app.add_exception_handler(RequestValidationError, handle_validation)
    app.add_exception_handler(StarletteHTTPException, handle_http)
    app.add_exception_handler(Exception, handle_unexpected)
