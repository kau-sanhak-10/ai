from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
