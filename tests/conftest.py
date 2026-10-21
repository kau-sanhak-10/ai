import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    # 개인 .env나 셸 환경변수(실제 키 포함)가 테스트 결과를 바꾸지 않게 막는다
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    # 테스트마다 환경변수를 바꿔도 설정을 다시 읽도록 캐시를 비운다
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())
