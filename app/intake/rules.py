"""업무별 필수정보 및 보완 횟수 규칙. YAML의 JSON-compatible 표현만 사용한다."""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.intake.contracts import FieldKey, WorkCategory

RULES_PATH = Path(__file__).resolve().parents[2] / "rules" / "inquiry_types.yaml"


class IntakeRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1)
    followupLimit: int = Field(ge=0)
    initialQuestionCount: int = Field(ge=1)
    commonRequiredFields: list[FieldKey]
    conditionalRequiredFieldsByWorkCategory: dict[WorkCategory, list[FieldKey]]
    referenceFields: list[FieldKey]

    def required_fields(self, work_category: str | None) -> set[str]:
        required = set(self.commonRequiredFields)
        if work_category in self.conditionalRequiredFieldsByWorkCategory:
            required.update(self.conditionalRequiredFieldsByWorkCategory[work_category])
        return required

    @property
    def max_asks(self) -> int:
        return self.initialQuestionCount + self.followupLimit


def load_rules(path: Path = RULES_PATH) -> IntakeRules:
    """외부 YAML 파서 의존 없이 YAML 호환 JSON 문법으로 작성한 규칙을 읽는다.

    규칙이 없거나 깨졌다면 임의 기본값으로 조용히 진행하지 않는다.
    """

    def no_duplicate_keys(items: list[tuple[str, object]]) -> dict[str, object]:
        result = dict(items)
        if len(result) != len(items):
            raise ValueError("규칙 키가 중복되었습니다")
        return result

    try:
        return IntakeRules.model_validate(
            json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        )
    except (OSError, ValueError, ValidationError) as exc:
        raise RuntimeError("AI_RULES_UNAVAILABLE") from exc
