"""일로 LangGraph: 내 고객 문장으로 State 흐름 확인 (검증 전용).

프로젝트(ai) 루트에서 실행:
    uv run python try_my_intake_gemini.py --mode gemini --message "귀뚜라미 보일러 온수가 안 나와요"
    uv run python try_my_intake_gemini.py --mode gemini --image-path "boiler.png" \
        --message "보일러에 오류가 표시돼요"
    uv run python try_my_intake_gemini.py --mode mock --message "난방이 안 돼요" \
        --text-fact "symptom=난방이 안 돼요" --category REPAIR

real은 .env의 OPENAI_API_KEY / OPENAI_MODEL 사용, gemini는 GEMINI_API_KEY 사용.
Gemini는 --image-path 경로의 로컬 JPG/PNG 사진을 함께 분석할 수 있음.
mock은 문장 자체를 해석하지 않고 사용자가 직접 지정한 추출 결과만 사용함.
DB 변경, 예약 실행, 사장님 이관 실행, API 서버 호출은 하지 않음.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic_core import to_jsonable_python

from app.core.config import get_settings
from app.intake.context import make_text_input
from app.intake.contracts import (
    AnalyzeRequest,
    CategoryDecision,
    Evidence,
    RawObservation,
    Safety,
    Understanding,
)
from app.intake.graph import build_workflow
from app.intake.providers import (
    IMAGE_INSTRUCTIONS,
    TEXT_INSTRUCTIONS,
    ModelOutput,
    OpenAIExtractionProvider,
)
from app.intake.rules import load_rules
from app.llm.gemini_provider import read_image

FIELD_KEYS = {"manufacturer", "model", "boiler_type", "error_code", "symptom"}
PHOTO_FIELD_KEYS = FIELD_KEYS - {"symptom"}
CATEGORIES = ("INSPECTION", "REPAIR", "LEAK", "REPLACEMENT")


def key_value(text: str, *, allowed: set[str], arg_name: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or key not in allowed or not value.strip():
        raise ValueError(
            f"{arg_name}: KEY=VALUE 형식이 필요합니다. 지원 키: {', '.join(sorted(allowed))}"
        )
    return key, value.strip()


class ManualMockProvider:
    """임의의 문장을 이해하지 않고 직접 적은 후보값만 제공하는 테스트용 제공자."""

    model = "manual-mock-no-llm"
    input_tokens = 0
    output_tokens = 0

    def __init__(self, args: argparse.Namespace):
        self.args = args

    def extract_message(self, messages):
        observations = []
        latest = messages[-1]
        for item in self.args.text_fact:
            key, value = key_value(item, allowed=FIELD_KEYS, arg_name="--text-fact")
            observations.append(
                RawObservation(
                    fieldKey=key,
                    value=value,
                    source="CUSTOMER",
                    evidence={"messageId": latest.messageId},
                    observedText=value,
                )
            )
        return Understanding(
            observations=observations,
            intent=self.args.intent,
            workCategory=CategoryDecision(
                value=self.args.category,
                status="CONFIRMED" if self.args.category else "UNCONFIRMED",
            ),
            safety=Safety(
                flag=self.args.safety,
                reasons=["SAFETY"] if self.args.safety else [],
            ),
        )

    def analyze_image(self, photo):
        observations = []
        for item in self.args.image_fact:
            key, value = key_value(item, allowed=PHOTO_FIELD_KEYS, arg_name="--image-fact")
            observations.append(
                RawObservation(
                    fieldKey=key,
                    value=value,
                    source="PHOTO",
                    evidence={"photoId": photo.photoId},
                    observedText=value,
                )
            )
        for key in self.args.image_unreadable:
            observations.append(
                RawObservation(
                    fieldKey=key,
                    value=None,
                    source="PHOTO",
                    evidence={"photoId": photo.photoId},
                    unconfirmedReason="PHOTO_UNREADABLE",
                )
            )
        return Understanding(observations=observations)


class GeminiGraphTestProvider:
    """로컬 검증용 Gemini 어댑터: 기존 LangGraph의 ExtractionProvider 계약을 구현.

    서비스용 Bedrock/YOLO 경로를 변경하지 않으며, 로컬 이미지 파일은 사용자가
    지정한 경로만 읽고 네트워크 URL로 가져오지 않는다.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        image_path: Path | None = None,
        *,
        debug_extraction: bool = False,
    ):
        self.api_key = api_key
        self.model = model
        self.image_path = image_path
        self.input_tokens = 0
        self.output_tokens = 0
        self.debug_extraction = debug_extraction

    def _debug(self, stage: str, **details) -> None:
        if self.debug_extraction:
            print(
                f"[Gemini extraction: {stage}] " + json.dumps(details, ensure_ascii=False),
                file=sys.stderr,
            )

    def _generate(self, instructions: str, contents: list) -> ModelOutput:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=self.api_key,
            http_options=types.HttpOptions(
                timeout=30000, retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=instructions,
                    response_mime_type="application/json",
                    response_json_schema=ModelOutput.model_json_schema(),
                    temperature=0,
                ),
            )
            usage = getattr(response, "usage_metadata", None)
            self.input_tokens += getattr(usage, "prompt_token_count", 0) or 0
            self.output_tokens += getattr(usage, "candidates_token_count", 0) or 0
            self._debug("response", attempt=1, text=response.text)
            if not response.text:
                raise ValueError("Gemini에서 구조화된 출력이 반환되지 않았습니다")
            parsed = ModelOutput.model_validate_json(response.text)
            self._debug("parsed", output=parsed.model_dump(mode="json"))
            return parsed
        finally:
            client.close()

    def extract_message(self, messages) -> Understanding:
        return self._extract_text(messages, make_text_input(messages))

    def extract_request(self, request: AnalyzeRequest) -> Understanding:
        return self._extract_text(request.messages, make_text_input(request.messages, request))

    def _extract_text(self, messages, text: str) -> Understanding:
        from google.genai import types

        parsed = self._generate(TEXT_INSTRUCTIONS, [types.Part.from_text(text=text)])
        observations = []
        for item in parsed.values:
            if item.value is None and not item.unsure:
                self._debug("rejected", fieldKey=item.fieldKey, reason="null_value")
                continue
            if not item.observedText:
                self._debug("rejected", fieldKey=item.fieldKey, reason="missing_observed_text")
                continue
            # LLM이 제시한 '관찰 텍스트'가 실제 메시지에 있어야만 근거로 인정한다.
            matching = next(
                (
                    msg
                    for msg in reversed(messages)
                    if msg.sender == "CUSTOMER"
                    and (item.messageId is None or item.messageId == msg.messageId)
                    and item.observedText.strip()
                    and item.observedText in msg.content
                ),
                None,
            )
            if matching is None:
                self._debug(
                    "rejected",
                    fieldKey=item.fieldKey,
                    reason="observed_text_not_in_message",
                    observedText=item.observedText,
                )
                continue
            observations.append(
                RawObservation(
                    fieldKey=item.fieldKey,
                    value=None if item.unsure else item.value,
                    source="CUSTOMER",
                    evidence=Evidence(messageId=matching.messageId),
                    observedText=item.observedText,
                    unconfirmedReason="CUSTOMER_UNSURE" if item.unsure else None,
                    isCorrection=item.isCorrection,
                )
            )
        self._debug("accepted", candidates=len(parsed.values), observations=len(observations))
        intent = (
            parsed.intent
            if parsed.intent in {"ANSWER", "ASK_OWNER", "CALLBACK", "BOOK", "OTHER"}
            else "OTHER"
        )
        return Understanding(
            observations=observations,
            intent=intent,
            intentEvidence=parsed.intentEvidence,
            workCategory=parsed.workCategory,
            safety=parsed.safety,
        )

    def analyze_image(self, photo) -> Understanding:
        from google.genai import types

        if self.image_path is None:
            raise ValueError("Gemini 로컬 이미지 분석에는 --image-path가 필요합니다")
        image_bytes, mime_type = read_image(self.image_path)
        parsed = self._generate(
            IMAGE_INSTRUCTIONS,
            [
                types.Part.from_text(text="보일러 제품 사진의 문자만 확인해 주세요."),
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ],
        )
        observations = []
        for item in parsed.values:
            if item.fieldKey not in ("manufacturer", "model", "error_code", "boiler_type"):
                continue
            if item.value and item.observedText:
                observations.append(
                    RawObservation(
                        fieldKey=item.fieldKey,
                        value=item.value,
                        source="PHOTO",
                        evidence=Evidence(photoId=photo.photoId),
                        observedText=item.observedText,
                    )
                )
            elif item.unreadable:
                observations.append(
                    RawObservation(
                        fieldKey=item.fieldKey,
                        value=None,
                        source="PHOTO",
                        evidence=Evidence(photoId=photo.photoId),
                        unconfirmedReason="PHOTO_UNREADABLE",
                    )
                )
        return Understanding(observations=observations, safety=parsed.safety)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="직접 입력으로 일로 LangGraph State 추적")
    parser.add_argument("--mode", choices=("mock", "real", "gemini"), default="mock")
    parser.add_argument("--model", default="gemini-3.5-flash-lite", help="Gemini 모델 ID")
    parser.add_argument("--message", help="고객 문장. 생략하면 실행 중에 직접 입력")
    parser.add_argument("--category", choices=CATEGORIES, help="Mock 전용: 확인된 작업 유형")
    parser.add_argument(
        "--intent",
        choices=("ANSWER", "ASK_OWNER", "CALLBACK", "BOOK", "OTHER"),
        default="ANSWER",
        help="Mock 전용: 고객 의도",
    )
    parser.add_argument(
        "--text-fact",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Mock 전용: 고객 문자에서 추출됐다고 가정할 정보 (반복 가능)",
    )
    parser.add_argument(
        "--image-fact",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Mock 전용: 사진에서 추출됐다고 가정할 정보",
    )
    parser.add_argument(
        "--image-unreadable",
        action="append",
        choices=sorted(PHOTO_FIELD_KEYS),
        default=[],
        help="Mock 전용: 사진에서 판독 불가능한 정보",
    )
    parser.add_argument("--safety", action="store_true", help="Mock 전용: 안전 문제 발생 가정")
    parser.add_argument(
        "--existing",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Spring에 저장된 기존 확인값을 가상으로 지정",
    )
    parser.add_argument(
        "--ask-count",
        action="append",
        default=[],
        metavar="KEY=COUNT",
        help="Spring의 해당 항목 질문 누적 횟수 가상 지정",
    )
    parser.add_argument("--photo-url", help="OpenAI(real): 접근 가능한 HTTPS 임시 사진 URL")
    parser.add_argument("--image-path", type=Path, help="Gemini: 로컬 JPG/PNG 사진 파일 경로")
    parser.add_argument("--save", type=Path, help="상태를 JSON 파일로 기록 (기본은 기록 안 함)")
    parser.add_argument(
        "--debug-extraction",
        action="store_true",
        help="Gemini 원시 응답, 파싱 결과, 문자 관찰값 제외 사유를 stderr에 출력",
    )
    return parser


def main() -> None:
    parser = make_parser()
    args = parser.parse_args()
    message = (
        args.message if args.message is not None else input("고객 문의를 입력하세요: ").strip()
    )
    if not message:
        parser.error("고객 문의 문장은 비어 있을 수 없습니다")

    existing = []
    for entry in args.existing:
        key, value = key_value(entry, allowed=FIELD_KEYS, arg_name="--existing")
        existing.append(
            {"fieldKey": key, "value": value, "source": "CUSTOMER", "status": "CONFIRMED"}
        )
    ask_counts = {}
    for entry in args.ask_count:
        key, sep, value = entry.partition("=")
        if not sep or not (key in FIELD_KEYS or key == "work_category") or not value.isdigit():
            parser.error("--ask-count: KEY=0 또는 KEY=1 같은 형식이어야 합니다")
        ask_counts[key] = int(value)

    for entry in args.text_fact:
        key_value(entry, allowed=FIELD_KEYS, arg_name="--text-fact")
    for entry in args.image_fact:
        key_value(entry, allowed=PHOTO_FIELD_KEYS, arg_name="--image-fact")

    photo_needed = bool(
        args.photo_url or args.image_fact or args.image_unreadable or args.image_path
    )
    if args.mode in {"real", "gemini"}:
        if (
            args.text_fact
            or args.image_fact
            or args.image_unreadable
            or args.category
            or args.safety
            or args.intent != "ANSWER"
        ):
            parser.error("실제 모델 모드에서는 Mock 전용 설정을 사용할 수 없습니다")
        settings = get_settings()
        if args.mode == "real":
            if args.image_path:
                parser.error("OpenAI 모드는 --photo-url, Gemini는 --image-path를 사용하세요")
            if not settings.openai_api_key or not settings.openai_model:
                parser.error(".env에 OPENAI_API_KEY와 OPENAI_MODEL을 설정하세요")
            provider = OpenAIExtractionProvider(
                settings.openai_api_key.get_secret_value(), settings.openai_model
            )
        else:
            if args.photo_url:
                parser.error("Gemini 로컬 테스트는 --photo-url 대신 --image-path를 사용하세요")
            if (
                not settings.gemini_api_key
                or not settings.gemini_api_key.get_secret_value().strip()
            ):
                parser.error(".env에 GEMINI_API_KEY를 설정하세요")
            provider = GeminiGraphTestProvider(
                settings.gemini_api_key.get_secret_value(),
                args.model,
                args.image_path,
                debug_extraction=args.debug_extraction,
            )
    else:
        if args.image_path:
            parser.error("Mock에서는 --image-path가 아닌 --image-fact를 사용하세요")
        provider = ManualMockProvider(args)

    # Mock 테스트의 URL은 단순한 사진 식별값으로서 실제 네트워크 요청을 하지 않는다.
    photos = (
        [
            {
                "photoId": 56,
                "messageId": 900,
                "imageUrl": args.photo_url or "https://example.org/local-test-photo.png",
            }
        ]
        if photo_needed
        else []
    )
    request = AnalyzeRequest.model_validate(
        {
            "requestId": "custom-test-1",
            "inquiryId": 101,
            "ownerId": 7,
            "inputVersion": 1,
            "handlerMode": "AI",
            "safetyFlag": False,
            "business": {"activeWorkCategories": list(CATEGORIES), "autoBookingEnabled": True},
            "fields": existing,
            "askCounts": ask_counts,
            "messages": [
                {
                    "messageId": 900,
                    "sender": "CUSTOMER",
                    "content": message,
                    "createdAt": datetime.now(UTC).isoformat(),
                }
            ],
            "photos": photos,
        }
    )

    print(f"\n분석 방식: {args.mode.upper()} / 고객 메시지: {message}")
    if args.mode == "mock":
        print("[안내] 아래 추출값은 사용자가 지정한 가상값입니다. Mock은 문장을 이해하지 않습니다.")
    elif args.mode == "real":
        print("[안내] OpenAI API로 테스트 문장이 전송되며 과금될 수 있습니다.")
    else:
        print("[안내] Gemini API로 테스트 문장과 지정한 사진이 전송되며 과금될 수 있습니다.")

    graph = build_workflow(provider, load_rules())
    initial = {"request": request, "observations": []}
    changes = []
    snapshots = []
    for event in graph.stream(initial, stream_mode=["updates", "values"], version="v2"):
        if event["type"] == "updates":
            for node, changed in event["data"].items():
                changes.append((node, to_jsonable_python(changed)))
        elif event["type"] == "values":
            snapshots.append(to_jsonable_python(event["data"]))
    if len(snapshots) != len(changes) + 1:
        raise RuntimeError("실행 단계와 State 기록의 개수가 일치하지 않습니다")

    print("\n--- Node별 State 변경 ---")
    for index, (node, updated) in enumerate(changes, start=1):
        print(f"\n{index}. {node}  (변경된 항목: {', '.join(updated)})")
        for key, value in updated.items():
            if key == "result":
                print("  result: 최종 구조화 응답 생성")
            else:
                print(f"  {key}: {json.dumps(value, ensure_ascii=False)}")

    result = snapshots[-1].get("result")
    print("\n--- 최종 결과 ---")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result is None:
        raise RuntimeError("최종 AnalyzeResult가 생성되지 않았습니다")

    if args.save:
        record = {
            "scenario": "custom-input",
            "mode": args.mode,
            "input": to_jsonable_python(request),
            "steps": [
                {"node": node, "updates": changed, "state_after": snapshots[i + 1]}
                for i, (node, changed) in enumerate(changes)
            ],
            "result": result,
        }
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nState 기록 저장 완료: {args.save}")


if __name__ == "__main__":
    main()
