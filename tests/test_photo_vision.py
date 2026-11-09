from io import BytesIO
from unittest.mock import MagicMock, Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.intake.contracts import Message
from app.intake.photo_vision import decode_photo, validate_s3_photo_url
from app.intake.vision_rules import extract
from app.intake.vision_text import extract_with_text
from app.main import create_app
from tests.test_intake_graph import payload

BUCKET = "ilro-test-photos"
REGION = "ap-northeast-2"
QUERY = "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=" + "a" * 64
URL = f"https://{BUCKET}.s3.{REGION}.amazonaws.com/inquiries/8/photo.png?{QUERY}"


def test_only_signed_url_for_own_bucket():
    validate_s3_photo_url(URL, BUCKET, REGION)
    validate_s3_photo_url(
        f"https://s3.{REGION}.amazonaws.com/{BUCKET}/photo.png?{QUERY}", BUCKET, REGION
    )
    for forbidden in (
        URL.replace(BUCKET, "different"),
        URL.replace("amazonaws.com/", "amazonaws.com.example.org/"),
        URL.replace("amazonaws.com/", "amazonaws.com:444/"),
        URL.replace("https://", "https://user@"),
        URL.split("?")[0],
        f"https://s3.{REGION}.amazonaws.com/{BUCKET}-other/photo.png?{QUERY}",
    ):
        with pytest.raises(ValueError):
            validate_s3_photo_url(forbidden, BUCKET, REGION)


def test_only_s3_ok_response_is_read_and_no_redirect(monkeypatch):
    from app.common.errors import BusinessException
    from app.intake.photo_vision import fetch_photo

    connection = MagicMock()
    response = connection.getresponse.return_value.__enter__.return_value
    response.status = 302
    transport = Mock(return_value=connection)
    monkeypatch.setattr("app.intake.photo_vision.HTTPSConnection", transport)
    with pytest.raises(BusinessException):
        fetch_photo(URL, BUCKET, REGION, lambda: 5)
    transport.assert_called_once_with(f"{BUCKET}.s3.{REGION}.amazonaws.com", timeout=5.0)
    response.read1.assert_not_called()
    connection.close.assert_called_once()


def test_pillow_transparency_and_pixel_limits(monkeypatch):
    source = BytesIO()
    Image.new("RGBA", (2, 2), (10, 20, 30, 0)).save(source, format="PNG")
    assert decode_photo(source.getvalue()).getpixel((0, 0)) == (255, 255, 255)
    monkeypatch.setattr("app.intake.photo_vision.MAX_PIXELS", 3)
    from app.common.errors import BusinessException

    with pytest.raises(BusinessException):
        decode_photo(source.getvalue())


@pytest.mark.parametrize(("screen_text", "expected"), [("A6", "A6"), ("28", None), ("28℃", None)])
def test_only_explicit_display_error_is_reported(screen_text, expected):
    result, _ = extract(
        {
            "object_type": "BOILER_CONTROLLER",
            "photo_type": "CONTROLLER",
            "regions": [
                {
                    "id": "display",
                    "kind": "display",
                    "bbox": [0, 0, 1, 1],
                    "texts": [
                        {
                            "text": screen_text,
                            "score": 0.99,
                            "complete": True,
                            "bbox": [0.1, 0.1, 0.3, 0.3],
                        }
                    ],
                }
            ],
        }
    )
    assert result["error_code"] == expected


def test_controller_label_row_does_not_overwrite_boiler_or_display():
    texts = [
        ("실내온도조절기", [0.1, 0.1, 0.5, 0.2]),
        ("모델명", [0.1, 0.6, 0.28, 0.7]),
        ("CTRL-1", [0.32, 0.6, 0.50, 0.7]),
        ("A6", [0.5, 0.35, 0.63, 0.44]),
    ]
    tokens = [dict(text=text, bbox=bbox, score=0.99, complete=True) for text, bbox in texts]
    result = extract_with_text([{"class": "display", "bbox": [0.4, 0.3, 0.7, 0.5]}], tokens)
    assert (result["controller_model"], result["model"], result["error_code"]) == (
        "CTRL-1",
        None,
        "A6",
    )


def test_service_keeps_controller_model_separate_from_boiler_model(monkeypatch):
    monkeypatch.setenv("INTERNAL_KEY", "internal-test-key")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "text-model")
    monkeypatch.setenv("PHOTO_S3_BUCKET", BUCKET)
    monkeypatch.setenv("PHOTO_YOLO_MODEL", "test.pt")
    monkeypatch.setenv("PHOTO_OCR_MODELS_DIR", "models")
    photo = {"photoId": 27, "messageId": 1, "imageUrl": URL}
    data = BytesIO()
    Image.new("RGB", (24, 24)).save(data, format="PNG")
    fetch = Mock(return_value=data.getvalue())
    aws = Mock(side_effect=AssertionError("a photo-only request must not contact Bedrock"))
    monkeypatch.setattr("app.intake.photo_vision.fetch_photo", fetch)
    monkeypatch.setattr("app.intake.bedrock_provider.boto3.session.Session", aws)
    monkeypatch.setattr("app.intake.photo_vision._engines", lambda *_: (None, None))
    monkeypatch.setattr(
        "app.intake.photo_vision.extract_pixels",
        lambda *_: {
            "photo_type": "CONTROLLER",
            "manufacturer": "RINNAI",
            "model": "WRONG-1",
            "controller_model": "CTRL-1",
            "error_code": None,
        },
    )
    request = payload(photos=[photo])
    request.messages = [
        Message(messageId=1, sender="CUSTOMER", content="", createdAt="2026-11-10T00:00:00+09:00")
    ]
    client = TestClient(create_app())
    headers = {"X-Internal-Key": "internal-test-key"}
    path = "/internal/v1/inquiries/analyze"
    first = client.post(path, json=request.model_dump(mode="json"), headers=headers)
    assert first.status_code == 200
    result = first.json()["result"]
    assert result["analyzedPhotoIds"] == [27]
    assert [
        (f["fieldKey"], f["value"], f["unconfirmedReason"], f["evidence"]["photoId"])
        for f in result["facts"]
    ] == [
        ("manufacturer", "린나이", None, 27),
        ("controller_model", "CTRL-1", None, 27),
        ("error_code", None, "PHOTO_UNREADABLE", 27),
    ]
    request.processedPhotoIds = [27]
    again = client.post(path, json=request.model_dump(mode="json"), headers=headers)
    assert again.json()["result"]["analyzedPhotoIds"] == []
    assert fetch.call_count == 1
    aws.assert_not_called()

    request.processedPhotoIds = []
    request.photos[0].imageUrl = URL.replace(BUCKET, "different")
    invalid = client.post(path, json=request.model_dump(mode="json"), headers=headers)
    assert invalid.status_code == 400 and fetch.call_count == 1
