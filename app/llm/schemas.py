"""일로 Day 2: 보일러 문의에서 추출할 JSON 구조 정의."""

from typing import Literal

from pydantic import BaseModel, Field

FieldName = Literal["manufacturer", "model_name", "error_code", "symptom", "address"]


class FieldEvidence(BaseModel):
    """어떤 원본에서 어떤 문구를 읽었는지 표시한다."""

    field_name: FieldName = Field(description="근거가 대응하는 정보 항목")
    source: Literal["message", "image"] = Field(description="문자 메시지 또는 첨부 이미지")
    observed_text: str = Field(description="원본 문자나 사진에 실제 보이는 짧은 문자열")


class BoilerExtraction(BaseModel):
    """LLM 출력용 구조. 찾을 수 없으면 null로 반환하도록 지시한다."""

    manufacturer: str | None = Field(description="제조사. 불확실하면 null")
    model_name: str | None = Field(description="정확한 모델명. 불확실하면 null")
    error_code: str | None = Field(description="기기에 표시된 오류코드. 없으면 null")
    symptom: str | None = Field(description="고객이 설명한 증상. 없으면 null")
    address: str | None = Field(description="고객이 제공한 주소. 없으면 null")
    evidence: list[FieldEvidence] = Field(
        description="각 정보의 근거가 된 문자·이미지 속 짧은 문구"
    )
    visible_image_text: list[str] = Field(
        description="사진에서 직접 읽을 수 있는 텍스트. 사진이 없으면 빈 배열"
    )
    uncertainties: list[str] = Field(
        description="읽기 어렵거나 서로 충돌하여 확인이 필요한 항목/이유"
    )
