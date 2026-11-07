from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수와 .env에서 읽는 설정. 실제 키는 .env에만 두고 커밋하지 않는다."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    app_env: Literal["local", "dev", "prod"] = "local"

    # 모델 연동 코드에서 settings.gemini_api_key.get_secret_value()로 읽는다
    gemini_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    jev_api_key: SecretStr | None = None

    # Spring → AI 전용 내부 접근 키. 프로덕션에서 반드시 설정.
    internal_key: SecretStr | None = None
    # 실제 접근 가능한 직접 API 모델을 명시적으로 설정한다. Bedrock 모델/리전은 별도 검증.
    openai_model: str | None = None
    # Spring 시도당 30초보다 짧게 유지한다. 문자·사진·재시도 전체가 공유한다.
    intake_timeout_seconds: float = Field(default=25.0, gt=0, lt=30)


@lru_cache
def get_settings() -> Settings:
    return Settings()
