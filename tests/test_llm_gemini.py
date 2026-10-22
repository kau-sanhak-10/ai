"""실제 API 호출 없이 보일러 멀티모달 추출 데이터 검증만 테스트한다."""

from pathlib import Path

import pytest
from PIL import Image

from app.llm.gemini_provider import log_call, make_output, read_image, verify_evidence
from app.llm.schemas import BoilerExtraction


def sample_extraction() -> BoilerExtraction:
    """검증 테스트에서 재사용할 대표 보일러 추출 결과를 만든다."""
    return BoilerExtraction.model_validate(
        {
            "manufacturer": "경동나비엔",
            "model_name": "NCB562",
            "error_code": "03",
            "symptom": "난방이 안 됨",
            "address": None,
            "evidence": [
                {"field_name": "manufacturer", "source": "message", "observed_text": "경동나비엔"},
                {"field_name": "model_name", "source": "image", "observed_text": "NCB562"},
                {"field_name": "error_code", "source": "image", "observed_text": "03"},
                {"field_name": "symptom", "source": "message", "observed_text": "난방이 안 돼요"},
            ],
            "visible_image_text": ["NCB562", "03"],
            "uncertainties": [],
        }
    )


def test_json_schema_accepts_null_and_evidence():
    """주소 누락을 null로 허용하고 필드 근거 구조를 보존하는지 확인한다."""
    parsed = sample_extraction()
    assert parsed.address is None
    assert parsed.evidence[1].source == "image"


def test_evidence_keeps_observed_values():
    """근거가 있는 값은 유지하고 누락 필드는 검토 대상으로 표시하는지 검사한다."""
    parsed = verify_evidence(sample_extraction(), "경동나비엔 보일러, 난방이 안 돼요", True)
    assert parsed.model_name == "NCB562"
    assert parsed.error_code == "03"
    result = make_output(parsed, "test-model", "req-1", True, {})
    assert result["missing_fields"] == ["address"]
    assert result["needs_review"] is True


def test_without_image_clears_image_only_values():
    """사진 없이 호출했을 때 이미지 출처 값과 이미지 텍스트를 제거하는지 확인한다."""
    parsed = verify_evidence(sample_extraction(), "경동나비엔 보일러, 난방이 안 돼요", False)
    assert parsed.model_name is None
    assert parsed.error_code is None
    assert parsed.visible_image_text == []


def test_unsubstantiated_message_is_dropped():
    """메시지 원문에 없는 근거로 제시된 추출값을 제거하는지 검사한다."""
    parsed = sample_extraction()
    parsed.evidence[0].observed_text = "문자에 없는 제조사"
    parsed = verify_evidence(parsed, "경동나비엔 보일러, 난방이 안 돼요", True)
    assert parsed.manufacturer is None


def test_real_image_and_fake_image(tmp_path: Path):
    """유효한 PNG는 읽고, 이미지가 아닌 파일은 거부하는지 확인한다."""
    valid = tmp_path / "boiler.png"
    Image.new("RGB", (20, 20), "white").save(valid)
    data, mime = read_image(valid)
    assert data and mime == "image/png"
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"this-is-not-a-real-image")
    with pytest.raises(ValueError):
        read_image(fake)


def test_log_without_sensitive_text(tmp_path: Path):
    """호출 로그에는 요청 메타데이터만 기록되고 사용자 원문은 남지 않는지 검사한다."""
    target = tmp_path / "calls.csv"
    record = {
        "timestamp": "2026-10-13T10:00:00+09:00",
        "request_id": "req-1",
        "provider": "gemini",
        "model": "test-model",
        "has_image": True,
        "status": "success",
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "latency_ms": 500,
        "error_type": "",
        "error_code": "",
    }
    log_call(target, record)
    data = target.read_text(encoding="utf-8-sig")
    assert "req-1" in data
    assert "GEMINI_API_KEY" not in data
    assert "난방이 안 돼요" not in data


@pytest.mark.parametrize("outcome", ["success", "invalid_json", "empty", "quota", "server"])
def test_server_call_records_usage_and_failures(tmp_path, monkeypatch, outcome):
    """Verify server call logging across success and failure paths."""
    import csv
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from app.llm.gemini_provider import analyze_gemini

    monkeypatch.setenv("GEMINI_API_KEY", "server-secret")
    image_path = tmp_path / "sample.png"
    Image.new("RGB", (4, 4)).save(image_path)
    client = MagicMock()
    text = sample_extraction().model_dump_json()
    if outcome == "invalid_json":
        text = "not-json-private-response"
    elif outcome == "empty":
        text = ""
    client.models.generate_content.return_value = SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=100, candidates_token_count=20, total_token_count=125
        ),
    )
    if outcome in ("quota", "server"):
        error = RuntimeError("private-provider-error")
        error.code = 429 if outcome == "quota" else 503
        client.models.generate_content.side_effect = error
    log_path = tmp_path / "calls.csv"
    with patch("google.genai.Client", return_value=client) as factory:
        if outcome == "success":
            result = analyze_gemini("test message", image_path, log_path=log_path)
            assert result["usage"]["total_tokens"] == 125
        else:
            with pytest.raises((ValueError, RuntimeError)):
                analyze_gemini("test message", image_path, log_path=log_path)
    options = factory.call_args.kwargs
    assert options["api_key"] == "server-secret"
    assert options["http_options"].retry_options.attempts == 1
    client.models.generate_content.assert_called_once()
    assert len(client.models.generate_content.call_args.kwargs["contents"]) == 2
    client.close.assert_called_once()
    with log_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == ("success" if outcome == "success" else "failed")
    assert row["total_tokens"] == ("" if outcome in ("quota", "server") else "125")
    if outcome in ("quota", "server"):
        assert row["error_code"] == ("429" if outcome == "quota" else "503")
    for private in ("server-secret", "test message", "private-provider-error", "private-response"):
        assert private not in log_path.read_text(encoding="utf-8-sig")


def test_server_entrypoints_ignore_dotenv(tmp_path, monkeypatch):
    from app.llm.gemini_provider import analyze_gemini
    from app.llm.openai_provider import call_openai_from_environment

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "GEMINI_API_KEY=dotenv-secret\nOPENAI_API_KEY=dotenv-secret\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        analyze_gemini("test message", log_path=tmp_path / "calls.csv")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        call_openai_from_environment("gpt-4.1-mini", "test message")


def test_gemini_text_only_server_call(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from app.llm.gemini_provider import analyze_gemini

    monkeypatch.setenv("GEMINI_API_KEY", "server-secret")
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(
        text=sample_extraction().model_dump_json(), usage_metadata=None
    )
    with patch("google.genai.Client", return_value=client):
        result = analyze_gemini("test message", log_path=tmp_path / "calls.csv")
    assert len(client.models.generate_content.call_args.kwargs["contents"]) == 1
    assert result["usage"]["total_tokens"] is None
    assert result["extraction"]["model_name"] is None
