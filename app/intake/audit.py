"""대화·사진·비밀키·예외 원문 없이 분석 1회의 운영 메타데이터만 기록한다."""

import json
import logging

logger = logging.getLogger(__name__)


def configure_audit_logging() -> None:
    logger.setLevel(logging.INFO)
    if not logging.getLogger().handlers and not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)


def record_analysis(**metadata: object) -> None:
    logger.info(json.dumps({"event": "intake_analysis", **metadata}, ensure_ascii=False))
