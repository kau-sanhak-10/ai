from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """애플리케이션 설정. AWS 인증·리전은 boto3 기본 탐색에 맡긴다."""

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
    # 기존 OpenAI 직접 API 실험용. FastAPI 서비스에서는 사용하지 않는다.
    openai_model: str | None = None
    bedrock_model_id: str | None = None
    photo_s3_bucket: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    photo_s3_region: str = Field(default="ap-northeast-2", pattern=r"^[a-z]{2}(?:-[a-z]+)+-\d+$")
    photo_yolo_model: Path | None = None
    photo_yolo_prompts: Path | None = None
    photo_ocr_models_dir: Path | None = None
    photo_cpu_threads: int = Field(default=2, ge=1)
    # Spring 시도당 30초보다 짧게 유지한다. 문자·사진 전체가 공유하고 내부 재시도는 없다.
    intake_timeout_seconds: float = Field(default=25.0, gt=0, lt=30)

    @model_validator(mode="after")
    def require_photo_config(self):
        if any(
            (
                self.photo_s3_bucket,
                self.photo_yolo_model,
                self.photo_ocr_models_dir,
                self.photo_yolo_prompts,
            )
        ) and not all((self.photo_s3_bucket, self.photo_yolo_model, self.photo_ocr_models_dir)):
            raise ValueError("사진 분석에는 S3 버킷, YOLO 모델, OCR 모델 경로가 필요합니다")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
