"""S3 사진의 YOLO 영역과 한국어 OCR에서만 접수 항목을 추출한다."""

import os
import re
from collections.abc import Callable
from functools import lru_cache
from http.client import HTTPSConnection
from io import BytesIO
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageOps

from app.common.errors import BusinessException, ErrorCode
from app.core.config import Settings
from app.intake.contracts import Evidence, FieldKey, Photo, RawObservation, Understanding
from app.intake.vision_rules import extract
from app.intake.vision_text import extract_with_text, visual_regions

MAX_PHOTO_BYTES = 10_000_000
MAX_PIXELS = 32_000_000
MAKER_NAMES = {
    "KD_NAVIEN": "경동나비엔",
    "KITURAMI": "귀뚜라미",
    "RINNAI": "린나이",
    "DAESUNG": "대성쎌틱",
    "LOTTE": "롯데기공",
}
CLASS_ALIASES = {
    "controller": "boiler_controller",
    "nameplate": "boiler_nameplate",
}
# ponytail: one inference per process protects the shared CPU/memory budget.
# Revisit with isolated workers only after measuring concurrent Dev traffic.
_VISION_LOCK = Lock()


def validate_s3_photo_url(url: str, bucket: str, region: str) -> None:
    if any(ord(ch) <= 32 or ord(ch) == 127 or ch == "\\" for ch in url):
        raise ValueError("사진 URL 문자가 올바르지 않습니다")
    parsed = urlsplit(url)
    endpoint = f"s3.{region}.amazonaws.com"
    same_bucket = (parsed.hostname == f"{bucket}.{endpoint}" and len(parsed.path) > 1) or (
        parsed.hostname == endpoint
        and parsed.path.startswith(f"/{bucket}/")
        and len(parsed.path) > len(bucket) + 2
    )
    query = parse_qs(parsed.query, max_num_fields=32)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
        or not same_bucket
        or query.get("X-Amz-Algorithm") != ["AWS4-HMAC-SHA256"]
        or not re.fullmatch(r"[0-9a-fA-F]{64}", query.get("X-Amz-Signature", [""])[0])
        or len(query.get("X-Amz-Signature", [])) != 1
    ):
        raise ValueError("팀 S3 버킷의 임시 사진 URL만 허용합니다")


def fetch_photo(url: str, bucket: str, region: str, remaining: Callable[[], float]) -> bytes:
    validate_s3_photo_url(url, bucket, region)
    parsed = urlsplit(url)
    # TLS로 검증한 고정 AWS 호스트에 직접 GET한다. 프록시나 리디렉션은 따르지 않는다.
    connection = HTTPSConnection(parsed.hostname, timeout=min(5.0, remaining()))
    try:
        connection.request("GET", f"{parsed.path}?{parsed.query}")
        with connection.getresponse() as response:
            if response.status != 200:
                raise BusinessException(ErrorCode.AI_MODEL_ERROR)
            declared_length = response.length
            if declared_length is not None and declared_length > MAX_PHOTO_BYTES:
                raise BusinessException(ErrorCode.INVALID_INPUT)
            data = bytearray()
            while chunk := response.read1(min(65536, MAX_PHOTO_BYTES + 1 - len(data))):
                remaining()
                data.extend(chunk)
                if len(data) > MAX_PHOTO_BYTES:
                    raise BusinessException(ErrorCode.INVALID_INPUT)
            if declared_length is not None and len(data) != declared_length:
                raise BusinessException(ErrorCode.AI_MODEL_ERROR)
    finally:
        connection.close()
    return bytes(data)


def decode_photo(data: bytes) -> Image.Image:
    try:
        with Image.open(BytesIO(data)) as original:
            if (
                original.format not in {"JPEG", "PNG"}
                or original.width * original.height > MAX_PIXELS
            ):
                raise BusinessException(ErrorCode.INVALID_INPUT)
            image = ImageOps.exif_transpose(original)
            image.thumbnail((2048, 2048))
            rgb = Image.new("RGB", image.size, "white")
            rgba = image.convert("RGBA")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            return rgb
    except (OSError, Image.DecompressionBombError) as exc:
        raise BusinessException(ErrorCode.INVALID_INPUT) from exc


@lru_cache(maxsize=1)
def _engines(model_path: Path, prompts: Path | None, ocr_dir: Path):
    detector_path = ocr_dir / "PP-OCRv5_mobile_det"
    reader_path = ocr_dir / "korean_PP-OCRv5_mobile_rec"
    for path in (model_path, *(p / "inference.pdiparams" for p in (detector_path, reader_path))):
        if not path.is_file():
            raise RuntimeError("사진 모델 파일이 배치되지 않았습니다")
    if prompts is not None and not prompts.is_file():
        raise RuntimeError("YOLOE 클래스 임베딩 파일이 배치되지 않았습니다")
    os.environ.setdefault("YOLO_AUTOINSTALL", "False")
    os.environ.setdefault("YOLO_OFFLINE", "true")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
    import cv2
    import torch
    from paddleocr import PaddleOCR
    from ultralytics import YOLO, YOLOE

    torch.set_num_threads(2)
    cv2.setNumThreads(1)
    detector = YOLOE(str(model_path)) if prompts is not None else YOLO(str(model_path))
    if prompts is not None:
        detector.load_prompt_embeddings(prompts)
    names = {CLASS_ALIASES.get(name, name) for name in detector.names.values()}
    if (
        not {"boiler_body", "boiler_controller", "boiler_nameplate", "display", "brand_logo"}
        <= names
    ):
        raise RuntimeError("YOLO 가중치에 일로 사진 분석 클래스가 부족합니다")
    reader = PaddleOCR(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_detection_model_dir=str(detector_path),
        text_recognition_model_name="korean_PP-OCRv5_mobile_rec",
        text_recognition_model_dir=str(reader_path),
        device="cpu",
        cpu_threads=2,
        enable_mkldnn=False,
        text_det_limit_type="max",
        text_det_limit_side_len=960,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )
    return detector, reader


def _inside(inner, outer):
    return all(
        (
            inner[0] >= outer[0] - 0.015,
            inner[1] >= outer[1] - 0.015,
            inner[2] <= outer[2] + 0.015,
            inner[3] <= outer[3] + 0.015,
        )
    )


def extract_pixels(image: Image.Image, detector, reader, remaining: Callable[[], float]) -> dict:
    """YOLO 영역과 OCR 상자의 좌표를 같은 사진 위에서 대조한다."""
    import numpy as np

    width, height = image.size
    remaining()
    output = detector.predict(source=image, conf=0.15, imgsz=640, device="cpu", verbose=False)[0]
    detections = []
    for box in output.boxes:
        a, b, c, d = box.xyxy[0].tolist()
        bounds = [
            max(0.0, a / width),
            max(0.0, b / height),
            min(1.0, c / width),
            min(1.0, d / height),
        ]
        if bounds[0] < bounds[2] and bounds[1] < bounds[3]:
            label = output.names[int(box.cls[0].item())]
            detections.append(
                {
                    "class": CLASS_ALIASES.get(label, label),
                    "bbox": bounds,
                    "score": float(box.conf[0].item()),
                }
            )

    strong = [d for d in detections if d["score"] >= 0.50]
    objects = [d for d in strong if d["class"] in {"boiler_body", "boiler_controller"}]
    plates = [d for d in strong if d["class"] == "boiler_nameplate"]
    negatives = [d for d in strong if d["class"] in {"gas_meter", "water_meter", "ac_remote"}]
    targets = objects or plates
    if negatives or len(targets) > 1:
        return extract(
            {
                "object_type": "UNKNOWN",
                "photo_type": "NOT_BOILER" if negatives and not targets else "UNKNOWN",
                "regions": [],
            }
        )[0]

    remaining()
    # ponytail: one bounded OCR pass, then match text boxes to YOLO regions.
    # Consider crop re-reading only after the common request deadline is measured on Dev.
    tokens = []
    for result in reader.predict(input=np.asarray(image)[:, :, ::-1].copy()):
        rec = result.json
        rec = rec.get("res", rec)
        for raw, score, coords in zip(
            rec["rec_texts"], rec["rec_scores"], rec["rec_boxes"], strict=True
        ):
            a, b, c, d = (float(point) for point in coords)
            if raw.strip() and 1 < a < c < width - 1 and 1 < b < d < height - 1:
                tokens.append(
                    {
                        "text": raw.strip(),
                        "score": float(score),
                        "complete": True,
                        "bbox": [a / width, b / height, c / width, d / height],
                    }
                )
    remaining()
    if targets:
        target = targets[0]["class"]
        controller = target == "boiler_controller"
        region_types = {
            "brand_logo": "controller_logo" if controller else "body_logo",
            "display": "display",
            **({} if controller else {"boiler_nameplate": "nameplate"}),
        }
        regions = [
            {
                "kind": region_types[d["class"]],
                "bbox": d["bbox"],
                "id": str(i),
                "texts": [t for t in tokens if _inside(t["bbox"], d["bbox"])],
            }
            for i, d in enumerate(detections)
            if d["class"] in region_types and _inside(d["bbox"], targets[0]["bbox"])
        ]
        photo_type = {
            "boiler_controller": "CONTROLLER",
            "boiler_body": "BODY",
            "boiler_nameplate": "NAMEPLATE",
        }[target]
        primary = extract(
            {
                "object_type": "BOILER_CONTROLLER" if controller else "BOILER",
                "photo_type": photo_type,
                "regions": regions,
            }
        )[0]
        if any(primary[key] for key in ("manufacturer", "model", "error_code")):
            return primary
    return extract_with_text(detections, tokens, visual_regions(image))


def photo_observations(result: dict, photo_id: int) -> Understanding:
    keys: tuple[FieldKey, ...] = ("manufacturer", "model", "error_code")
    if result["photo_type"] == "NOT_BOILER":
        return Understanding()
    if result["photo_type"] == "CONTROLLER":
        keys = ("manufacturer", "error_code")  # 조절기 모델을 본체 model로 저장하지 않는다.
    observations = []
    for key in keys:
        value = result[key]
        if key == "manufacturer":
            value = MAKER_NAMES.get(value)
        elif (
            key == "model"
            and value
            and (
                len(value) > 60
                or not re.search(r"[A-Za-z가-힣]", value)
                or not re.search(r"\d", value)
                or re.search(r"문의|고객센터|(?<![A-Z])A\s*/\s*S(?![A-Z])", value, re.I)
            )
        ):
            value = None
        observations.append(
            RawObservation(
                fieldKey=key,
                value=value,
                source="PHOTO",
                evidence=Evidence(photoId=photo_id),
                unconfirmedReason=None if value else "PHOTO_UNREADABLE",
            )
        )
    return Understanding(observations=observations)


def analyze_photo(
    photo: Photo, remaining: Callable[[], float], *, settings: Settings
) -> Understanding:
    assert settings.photo_s3_bucket and settings.photo_yolo_model and settings.photo_ocr_models_dir
    if not _VISION_LOCK.acquire(timeout=remaining()):
        raise TimeoutError("사진 분석 대기 시간 초과")
    try:
        data = fetch_photo(
            photo.imageUrl, settings.photo_s3_bucket, settings.photo_s3_region, remaining
        )
        image = decode_photo(data)
        remaining()
        detector, reader = _engines(
            settings.photo_yolo_model,
            settings.photo_yolo_prompts,
            settings.photo_ocr_models_dir,
        )
        result = extract_pixels(image, detector, reader, remaining)
        remaining()
        return photo_observations(result, photo.photoId)
    finally:
        _VISION_LOCK.release()


def warm_vision(settings: Settings) -> None:
    assert settings.photo_yolo_model and settings.photo_ocr_models_dir
    detector, reader = _engines(
        settings.photo_yolo_model, settings.photo_yolo_prompts, settings.photo_ocr_models_dir
    )
    extract_pixels(Image.new("RGB", (640, 640), "white"), detector, reader, lambda: None)
