from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI

from app.api import health, inquiries
from app.common.errors import register_exception_handlers
from app.core.config import get_settings
from app.intake.audit import configure_audit_logging
from app.intake.photo_vision import warm_vision


def create_app() -> FastAPI:
    configure_audit_logging()
    settings = get_settings()
    # API 문서(/docs)는 운영 환경에서 닫는다
    docs_enabled = settings.app_env != "prod"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.photo_s3_bucket:
            await anyio.to_thread.run_sync(warm_vision, settings)
        yield

    app = FastAPI(
        title="일로 AI 서버",
        version="0.1.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(inquiries.router)
    return app


app = create_app()
