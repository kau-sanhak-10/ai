"""실제 OpenAI API 비용 없이 입력 빌드/SDK 호출/응답 검증 테스트."""

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from app.llm.openai_provider import (
    _error_details,
    call_openai_from_environment,
    call_openai_multimodal,
    make_content,
    summarize_calls,
)
from app.llm.schemas import BoilerExtraction, FieldEvidence


def test_text_content():
    """문자만 입력하면 프롬프트가 포함된 텍스트 파트 하나를 만드는지 확인한다."""
    parts = make_content("난방이 안 돼요")
    assert len(parts) == 1
    assert parts[0]["type"] == "input_text"
    assert "난방이 안 돼요" in parts[0]["text"]


def test_image_content(tmp_path):
    """사진을 추가하면 올바른 MIME 데이터 URL과 이미지 콘텐츠를 만드는지 검사한다."""
    p = tmp_path / "sample.png"
    Image.new("RGB", (4, 4)).save(p)
    parts = make_content("사진의 글씨", p)
    assert len(parts) == 2
    assert parts[1]["type"] == "input_image"
    prefix = "data:image/png;base64,"
    assert parts[1]["image_url"].startswith(prefix)
    assert base64.b64decode(parts[1]["image_url"][len(prefix) :]) == p.read_bytes()


def test_unsupported_image(tmp_path):
    """지원하지 않는 파일 형식이 입력 단계에서 거부되는지 확인한다."""
    path = tmp_path / "sample.txt"
    path.write_text("not image")
    with pytest.raises(ValueError):
        make_content("확인", path)


def test_empty_message():
    """공백뿐인 고객 메시지를 API 호출 전에 거부하는지 검사한다."""
    with pytest.raises(ValueError):
        make_content("   ")


def test_parse_call_and_usage(tmp_path):
    """모의 OpenAI 응답을 구조화 모델로 받고 토큰 및 요청 로그를 보존하는지 확인한다."""
    fake = BoilerExtraction(
        manufacturer="Navien",
        model_name=None,
        error_code=None,
        symptom="난방이 안 돼요",
        address=None,
        evidence=[
            FieldEvidence(field_name="symptom", source="message", observed_text="난방이 안 돼요")
        ],
        visible_image_text=[],
        uncertainties=[],
    )
    response = SimpleNamespace(
        output_parsed=fake,
        usage=SimpleNamespace(input_tokens=12, output_tokens=20, total_tokens=32),
    )
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.responses.parse.return_value = response
    with patch.dict(
        "sys.modules", {"openai": SimpleNamespace(OpenAI=MagicMock(return_value=mock_client))}
    ):
        from openai import OpenAI as sdk

        extraction, usage = call_openai_multimodal(
            "fake_test_key",
            "gpt-4.1-mini",
            "난방이 안 돼요",
            usage_db_path=tmp_path / "usage.sqlite3",
        )
    sdk.assert_called_once_with(api_key="fake_test_key", timeout=30.0, max_retries=0)
    args = mock_client.responses.parse.call_args.kwargs
    assert args["model"] == "gpt-4.1-mini"
    assert args["text_format"] is BoilerExtraction
    assert args["input"][0]["content"][0]["type"] == "input_text"
    assert extraction.symptom == "난방이 안 돼요"
    assert usage["total_tokens"] == 32
    assert usage["request_id"]
    logged = summarize_calls(tmp_path / "usage.sqlite3")
    assert (logged["calls"], logged["success"], logged["failed"]) == (1, 1, 0)
    assert logged["tokens"]["total_tokens"] == 32
    assert logged["requests"][0]["request_id"] == usage["request_id"]
    assert "fake_test_key" not in str(logged)
    assert "난방이 안 돼요" not in str(logged)


def test_missing_parsed_response(tmp_path):
    """구조화 결과가 없는 응답은 실패로 로그되고 오류를 발생시키는지 검사한다."""
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.responses.parse.return_value = SimpleNamespace(output_parsed=None)
    with (
        patch.dict(
            "sys.modules", {"openai": SimpleNamespace(OpenAI=MagicMock(return_value=mock_client))}
        ),
        pytest.raises(ValueError, match="구조화 출력"),
    ):
        call_openai_multimodal(
            "fake_test_key",
            "gpt-4.1-mini",
            "확인",
            usage_db_path=tmp_path / "usage.sqlite3",
        )
    logged = summarize_calls(tmp_path / "usage.sqlite3")
    assert (logged["calls"], logged["failed"]) == (1, 1)
    assert logged["requests"][0]["error_type"] == "ValueError"


def test_missing_key():
    """빈 비밀키로 OpenAI 클라이언트를 생성하지 않고 입력 오류를 내는지 확인한다."""
    with pytest.raises(ValueError, match="API_KEY"):
        call_openai_multimodal(" ", "gpt-4.1-mini", "확인")


def test_server_entrypoint_reads_key_from_environment(tmp_path, monkeypatch):
    """서버 호출 경로가 키를 환경변수에서만 읽어 SDK에 전달하는지 검사한다."""
    monkeypatch.setenv("OPENAI_API_KEY", "server_secret")
    fake = BoilerExtraction(
        manufacturer=None,
        model_name=None,
        error_code=None,
        symptom=None,
        address=None,
        evidence=[],
        visible_image_text=[],
        uncertainties=[],
    )
    response = SimpleNamespace(
        output_parsed=fake,
        usage=SimpleNamespace(input_tokens=1, output_tokens=2, total_tokens=3),
    )
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.responses.parse.return_value = response
    mock_openai = MagicMock(return_value=mock_client)
    with patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=mock_openai)}):
        _, usage = call_openai_from_environment(
            "gpt-4.1-mini", "확인", usage_db_path=tmp_path / "usage.sqlite3"
        )
    mock_openai.assert_called_once_with(api_key="server_secret", timeout=30.0, max_retries=0)
    assert usage["total_tokens"] == 3
    assert "server_secret" not in str(summarize_calls(tmp_path / "usage.sqlite3"))


def test_quota_error_is_logged_without_retry(tmp_path):
    """할당량 초과는 재시도되지 않고 민감한 원문 없이 분류되어 저장되는지 확인한다."""
    quota_error = RuntimeError("Do not persist this provider message")
    quota_error.status_code = 429
    quota_error.body = {
        "error": {
            "code": "insufficient_quota",
            "message": "Do not persist this provider message",
        }
    }
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.responses.parse.side_effect = quota_error
    mock_openai = MagicMock(return_value=mock_client)
    usage_db = tmp_path / "usage.sqlite3"
    with (
        patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=mock_openai)}),
        pytest.raises(RuntimeError, match="Do not persist"),
    ):
        call_openai_multimodal(
            "fake_test_key",
            "gpt-4.1-mini",
            "고객 입력",
            usage_db_path=usage_db,
        )
    mock_client.responses.parse.assert_called_once()
    mock_openai.assert_called_once_with(api_key="fake_test_key", timeout=30.0, max_retries=0)
    logged = summarize_calls(usage_db)
    assert logged["requests"][0]["error_category"] == "quota_exhausted"
    assert logged["requests"][0]["error_code"] == "insufficient_quota"
    assert "fake_test_key" not in str(logged)
    assert "고객 입력" not in str(logged)
    assert "Do not persist this provider message" not in str(logged)


def test_rate_limit_error_is_classified_separately():
    """HTTP 429의 속도 제한 코드를 크레딧 소진과 별도 분류하는지 검사한다."""
    error = RuntimeError("rate limited")
    error.status_code = 429
    error.body = {"error": {"code": "rate_limit_exceeded"}}
    assert _error_details(error) == ("RuntimeError", "rate_limit_exceeded", "rate_limited")


def test_usage_summary_without_database(tmp_path):
    """요청 DB가 없을 때 사용량 요약이 빈 상태를 반환하는지 확인한다."""
    assert summarize_calls(tmp_path / "missing.sqlite3") == {
        "found": False,
        "calls": 0,
        "success": 0,
        "failed": 0,
        "tokens": {},
    }
