# Dev 상담 연결 확인

1. Dev AI 컨테이너에 `BEDROCK_MODEL_ID`, AWS 리전, Spring과 동일한 `INTERNAL_KEY`를 설정한다. AWS 인증은 EC2 역할을 사용하며 Bedrock `Converse`에 필요한 `bedrock:InvokeModel` 권한은 실제 추론 프로파일·모델로 제한한다. 글로벌 프로파일은 처리 리전 범위가 달라질 수 있으므로 첨부본의 예시를 그대로 배포 기본값으로 두지 않는다.
2. 승인된 Dev 컨테이너 안에서 다음 합성 문의를 한 번 실행한다. 실제 고객 정보나 사진을 쓰지 않는다.

```sh
python - <<'PY'
import json
import urllib.request
from app.core.config import get_settings

settings = get_settings()
assert settings.app_env == "dev" and settings.internal_key and settings.bedrock_model_id
data = {
    "requestId": "dev-text-check", "inquiryId": 1, "ownerId": 1,
    "inputVersion": 1, "handlerMode": "AI",
    "business": {"activeWorkCategories": ["REPAIR"], "autoBookingEnabled": True},
    "fields": [], "askCounts": {}, "photos": [], "safetyFlag": False,
    "messages": [{"messageId": 1, "sender": "CUSTOMER",
                  "content": "보일러 온수가 안 나와서 수리를 받고 싶어요.",
                  "createdAt": "2026-11-10T00:00:00+09:00"}],
}
request = urllib.request.Request(
    "http://127.0.0.1:8000/internal/v1/inquiries/analyze",
    data=json.dumps(data).encode(),
    headers={"Content-Type": "application/json",
             "X-Internal-Key": settings.internal_key.get_secret_value()},
)
with urllib.request.urlopen(request, timeout=30) as response:
    body = json.load(response)
result = body["result"]
assert body["success"] and result["modelName"] == settings.bedrock_model_id
assert result["usage"]["inputTokens"] > 0 and not result["analyzedPhotoIds"]
assert any(f["fieldKey"] == "symptom" and f["evidence"]["messageId"] == 1
           for f in result["facts"])
print(result["modelName"], result["nextAction"]["action"], result["usage"])
PY
```

3. 위 검사는 FastAPI와 Bedrock만 확인한다. QA-02는 별도로 승인된 Dev 테스트 업체 링크에서 고객 문의를 보내고, **같은 문의 ID**가 Spring에 저장돼 사장님 화면에서 열리는지 확인한다. 새 문의·실제 문자는 팀 테스트 계정에서만 사용한다.
4. 사진 실연은 [사진 분석 설정](photo_intake.md)을 적용하고, 실제 S3 임시 URL에서 얻은 추출값 또는 ‘미확인’과 사진 ID를 Spring이 저장·조회하는 것까지 확인한 뒤 진행한다.

## Spring 연결 전환 및 합격 기준

- 시연용 Dev 문의에서는 Spring의 `AI_ENABLED=true`, `AI_MODE=http`, `AI_BASE_URL`(백엔드에서 도달 가능한 Dev FastAPI 주소), `AI_INTERNAL_KEY`(FastAPI의 `INTERNAL_KEY`와 동일)를 적용한다. 환경값은 팀의 기존 비밀 설정 경로로 관리하고 값 자체를 코드·PR·QA 기록에 넣지 않는다. 먼저 FastAPI의 위 내부 호출을 검증한 뒤 Spring을 재배포한다.
- 11/10 공유 Dev 합성 문의 #7은 원문 저장과 AI 답변 조회에 성공했으나, 답변이 `[가짜 응답]`으로 시작했다. 공개 health가 UP이거나 고객 화면에 답변이 나타나도 Bedrock 연결로 판정하지 않는다.
- 전환 뒤 새 합성 문의에서 FastAPI의 Bedrock 모델명·사용량이 기록된 분석과 Spring의 같은 문의 분석 결과를 대조한다. 시연 업체는 제공 작업 유형(예: 수리)과 접수 상태를 확인한다. 작업 유형이 비어 있으면 사진·대화 연결과 별개로 정상 수리 흐름을 대표하지 못한다.
- 실제 고객 데이터, 문자 코드, 서명 URL, 고객 또는 사장님 토큰은 증빙에 넣지 않는다. 예약 확정은 중간발표 실연의 합격 조건에 넣지 않는다.

[Converse 권한](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html) · [추론 프로파일 리전](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html)
