"""AI-02: 원문 대화를 분할하지 않고 Spring의 최신 접수 상태와 함께 전달한다."""

import json
import re

from app.intake.contracts import AnalyzeRequest, Message

PROMPT_VERSION = "intake-ai02-v2"


def make_text_input(messages: list[Message], request: AnalyzeRequest | None = None) -> str:
    # 식별자·업체/고객정보·사진 URL은 모델의 대화 이해에 필요하지 않다.
    context = {
        "fields": [field.model_dump(exclude_none=True) for field in request.fields]
        if request
        else [],
        "askCounts": request.askCounts if request else {},
        "messages": [
            {"messageId": m.messageId, "sender": m.sender, "content": m.content} for m in messages
        ],
    }
    return json.dumps(context, ensure_ascii=False)


def is_explicit_correction(content: str) -> bool:
    """단순히 다른 값을 말한 경우와 앞선 설명을 명시적으로 정정한 경우를 구분한다."""
    return bool(re.search(r"아니라|아니고|정정|잘못\s*(?:말|알|봤|보)|착각|다시\s*보니", content))
