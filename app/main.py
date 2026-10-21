from fastapi import FastAPI

from app.api import health
from app.common.errors import register_exception_handlers
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    # API 문서(/docs)는 운영 환경에서 닫는다
    docs_enabled = settings.app_env != "prod"

    app = FastAPI(
        title="일로 AI 서버",
        version="0.1.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    return app


app = create_app()
