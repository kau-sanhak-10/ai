"""일로 WBS v1.1: Mock 계약·규칙 전용 테스트. 실제 모델 성능 테스트가 아님.
실행: 레포 루트에서 uv run pytest -q tests/test_wbs_mock_cases.py
"""

import json
from pathlib import Path

import pytest

from app.intake.contracts import (
    AnalyzeRequest,
    CategoryDecision,
    RawObservation,
    Safety,
    Understanding,
)
from app.intake.graph import build_workflow
from app.intake.rules import load_rules

FIXTURES = Path(__file__).resolve().parents[1] / "eval" / "08_ds05_mock_fixtures_v1_1.jsonl"
CASES = [
    json.loads(line) for line in FIXTURES.read_text(encoding="utf-8").splitlines() if line.strip()
]


class MockExtractionProvider:
    model = "wbs-v1.1-mock"
    input_tokens = 0
    output_tokens = 0

    def __init__(self, case):
        self.case = case

    def extract_message(self, messages):
        data = self.case["mockProvider"]
        return Understanding(
            observations=[RawObservation.model_validate(item) for item in data["textObservations"]],
            intent=data["intent"],
            workCategory=CategoryDecision.model_validate(data["workCategory"]),
            safety=Safety.model_validate(data["safety"]),
        )

    def analyze_image(self, photo):
        data = self.case["mockProvider"]
        return Understanding(
            observations=[RawObservation.model_validate(item) for item in data["photoObservations"]]
        )


@pytest.mark.parametrize("case", CASES, ids=lambda r: r["caseId"])
def test_wbs_routing_contract(case):
    req = AnalyzeRequest.model_validate(case["request"])
    result = build_workflow(MockExtractionProvider(case), load_rules()).invoke(
        {"request": req, "observations": []}
    )["result"]
    assert result.nextAction.action == case["expected"]["nextAction"]
    assert result.nextAction.targetFieldKeys == case["expected"]["targetFieldKeys"]
    assert result.safety.flag == case["expected"]["safetyFlag"]
    assert result.inputVersion == req.inputVersion
    assert result.decidedBy == "RULE"
    if case["caseId"] == "DEV-007":
        assert not any(f.fieldKey == "model" and f.value != "A-100" for f in result.facts)
    if case["caseId"] == "MOCK-REF-AT-LIMIT":
        assert any(
            f.fieldKey == "manufacturer" and f.unconfirmedReason == "CONFLICT" for f in result.facts
        )
    if case["caseId"] == "MOCK-PHOTO-UNREADABLE":
        assert any(f.fieldKey == "model" and f.status == "UNCONFIRMED" for f in result.facts)
