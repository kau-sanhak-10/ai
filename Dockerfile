# AI 서버(FastAPI) 배포 이미지 - EC2 ilro-ai에서 실행, 백엔드만 8000번으로 호출 (보안 그룹 ilro-ai-sg)
# Bedrock은 EC2 Role 권한으로 호출 - AWS 키를 이미지·환경변수에 넣지 않음

# 1단계: 의존성 설치 (uv.lock 그대로)
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# 의존성 파일만 먼저 복사 - 코드만 바뀌면 이 단계는 캐시를 그대로 씀
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY rules ./rules

# 2단계: 실행용 (uv·빌드 도구 없이 가볍게)
FROM python:3.12-slim
# YOLO·OCR(OpenCV)용 시스템 라이브러리 - 패키지가 정해지면 필요한 것만 주석 해제
# RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=3).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
