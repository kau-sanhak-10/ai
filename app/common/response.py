from pydantic import BaseModel


class ApiResponse[T](BaseModel):
    """백엔드 ApiResponse와 같은 형식: {success, code, message, result}."""

    success: bool
    code: str
    message: str
    result: T | None = None

    @classmethod
    def ok(cls, result: T | None = None) -> "ApiResponse[T]":
        return cls(success=True, code="OK", message="요청 성공", result=result)


class FieldErrorResponse(BaseModel):
    field: str
    reason: str
