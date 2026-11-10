"""Bedrock Converse 기반 Claude 어댑터. AWS 인증·리전은 SDK 기본 설정을 따른다."""

import time
from collections.abc import Callable
from contextlib import closing
from functools import partial

import anyio
import boto3.session
from botocore.config import Config

from app.intake.contracts import Photo, Understanding
from app.intake.providers import ModelOutput, StructuredExtractionProvider

TOOL_NAME = "submit_extraction"


class BedrockExtractionProvider(StructuredExtractionProvider):
    def __init__(
        self,
        model: str,
        *,
        timeout_seconds: float = 25.0,
        photo_analyzer: Callable[[Photo, Callable[[], float]], Understanding] | None = None,
    ):
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._photo_analyzer = photo_analyzer
        self.input_tokens = 0
        self.output_tokens = 0
        self.model_calls = 0
        self.usage_reported_calls = 0
        self._deadline = time.monotonic() + timeout_seconds

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("AI 분석 시간 예산을 소진했습니다")
        return remaining

    def analyze_image(self, photo: Photo) -> Understanding | None:
        if self._photo_analyzer is None:
            return None
        return self._photo_analyzer(photo, self._remaining)

    async def aanalyze_image(self, photo: Photo) -> Understanding | None:
        if self._photo_analyzer is None:
            return None
        return await anyio.to_thread.run_sync(self.analyze_image, photo, abandon_on_cancel=True)

    def _invoke(self, instructions: str, content: list[dict]) -> dict:
        if any(entry["type"] != "input_text" for entry in content):
            raise ValueError("Bedrock에는 텍스트만 전달할 수 있습니다")
        blocks = [{"text": entry["text"]} for entry in content]
        remaining = self._remaining()
        connect_timeout = min(3.0, remaining / 2)
        config = Config(
            connect_timeout=connect_timeout,
            read_timeout=remaining - connect_timeout,
            retries={"mode": "standard", "total_max_attempts": 1},
        )
        schema = ModelOutput.model_json_schema()
        schema["required"] = list(schema["properties"])
        # 요청별 Session으로 기본 인증 탐색을 사용한다. 키·프로파일·리전을 전달하지 않는다.
        with closing(boto3.session.Session().client("bedrock-runtime", config=config)) as client:
            # 인증 탐색 중 시간이 끝났다면 모델 요청을 새로 보내지 않는다.
            self._remaining()
            self.model_calls += 1
            return client.converse(
                modelId=self.model,
                system=[{"text": instructions}],
                messages=[{"role": "user", "content": blocks}],
                inferenceConfig={"maxTokens": 2048},
                additionalModelRequestFields={"thinking": {"type": "disabled"}},
                toolConfig={
                    "tools": [
                        {
                            "toolSpec": {
                                "name": TOOL_NAME,
                                "description": "고객 대화에서 확인한 정보만 제출합니다.",
                                "inputSchema": {"json": schema},
                            }
                        }
                    ],
                    "toolChoice": {"tool": {"name": TOOL_NAME}},
                },
            )

    def _validated_output(self, response: dict) -> ModelOutput:
        usage = response.get("usage")
        if isinstance(usage, dict):
            self.usage_reported_calls += 1
            self.input_tokens += usage.get("inputTokens", 0) or 0
            self.output_tokens += usage.get("outputTokens", 0) or 0
        if response.get("stopReason") != "tool_use":
            raise ValueError("Claude 구조화 출력이 없거나 중단되었습니다")
        try:
            tools = [
                block["toolUse"]
                for block in response["output"]["message"]["content"]
                if "toolUse" in block
            ]
            if len(tools) != 1 or tools[0]["name"] != TOOL_NAME:
                raise ValueError("Claude 구조화 출력 도구가 일치하지 않습니다")
            output = tools[0]["input"]
            required = ModelOutput.model_fields.keys()
            if not isinstance(output, dict) or not required <= output.keys():
                raise ValueError("Claude 구조화 출력에 필수 항목이 없습니다")
            if output["intent"] not in {"ANSWER", "ASK_OWNER", "CALLBACK", "BOOK", "OTHER"}:
                raise ValueError("허용하지 않는 고객 의도입니다")
            return ModelOutput.model_validate(output)
        except (KeyError, TypeError) as exc:
            raise ValueError("Claude 구조화 출력 형식이 잘못되었습니다") from exc

    def _parse(self, instructions: str, content: list[dict]) -> ModelOutput:
        response = self._invoke(instructions, content)
        self._remaining()
        return self._validated_output(response)

    async def _aparse(self, instructions: str, content: list[dict]) -> ModelOutput:
        # 대기 취소는 동기 스레드나 AWS 측 추론 중단을 보장하지 않는다.
        response = await anyio.to_thread.run_sync(
            partial(self._invoke, instructions, content), abandon_on_cancel=True
        )
        self._remaining()
        return self._validated_output(response)
