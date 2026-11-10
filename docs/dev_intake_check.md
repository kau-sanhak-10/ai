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

- AI [#12](https://github.com/kau-sanhak-10/ai/pull/12)는 현재 #9 브랜치를 대상으로 한 Draft다. #7과 #9를 따로 합치는 경우 README 충돌을 먼저 해소한다. #12 후보에는 두 PR의 커밋과 README 해결이 이미 포함돼 있으므로, 팀이 한 번에 리뷰하기로 합의하면 `develop` 대상 통합 PR로 검토할 수 있다. 어느 쪽이든 최종 대상의 CI를 통과시킨 뒤 배포한다.
- 백엔드 `develop`에 병합하면 CI 통과 후 공유 Dev 배포가 시작된다. 병합 승인 전에 FastAPI 이미지·설정과 복구 절차를 준비한다. AI 저장소의 GitHub CI는 검사만 하며 FastAPI를 배포하지 않는다.
- 사진 전환은 [사진 분석 설정](photo_intake.md) 순서를 따른다. 새 FastAPI 이미지(사진 의존성 포함)를 사진 설정 없이 먼저 올리고 위 Bedrock 텍스트 검사를 통과시킨다. 호환 Spring이 배포된 다음 FastAPI의 사진 설정을 켜고, 모델 워밍업이 끝난 뒤 실제 S3 왕복을 확인한다. `/health`만으로 사진 또는 Bedrock 연결을 판정하지 않는다.
- 시연용 Dev 문의에서는 Spring의 `AI_ENABLED=true`, `AI_MODE=http`, `AI_BASE_URL`(백엔드에서 도달 가능한 Dev FastAPI 주소), `AI_INTERNAL_KEY`(FastAPI의 `INTERNAL_KEY`와 동일)를 적용한다. 환경값은 팀의 기존 비밀 설정 경로로 관리하고 값 자체를 코드·PR·QA 기록에 넣지 않는다. 먼저 FastAPI의 위 내부 호출을 검증한 뒤 Spring을 재배포한다.
- 11/10 공유 Dev 합성 문의 #7은 원문 저장과 AI 답변 조회에 성공했으나, 답변이 `[가짜 응답]`으로 시작했다. 공개 health가 UP이거나 고객 화면에 답변이 나타나도 Bedrock 연결로 판정하지 않는다.
- 전환 뒤 새 합성 문의에서 FastAPI의 Bedrock 모델명·사용량이 기록된 분석과 Spring의 같은 문의 분석 결과를 대조한다. 시연 업체는 제공 작업 유형(예: 수리)과 접수 상태를 확인한다. 작업 유형이 비어 있으면 사진·대화 연결과 별개로 정상 수리 흐름을 대표하지 못한다.
- 안전 예외를 확인할 때는 다른 문의와 같은 문의를 구분한다. 이전 Spring은 실행 중인 문의의 새 메시지가 앞선 호출을 기다렸다. 수정 후보는 같은 문의에 더 최신 고객 문장이 저장되면 대기 중인 사진 응답을 포기하고 최신 문의를 다시 읽는다. 이전 사진 응답과 근거가 적용되지 않고, 안전 신고 원문·사장님 확인·알림이 한 번만 남는지 Dev에서 확인한다. 이는 이미 시작된 OCR 작업을 즉시 종료한다는 의미가 아니다.
- 실제 고객 데이터, 문자 코드, 서명 URL, 고객 또는 사장님 토큰은 증빙에 넣지 않는다. 예약 확정은 중간발표 실연의 합격 조건에 넣지 않는다.

[Converse 권한](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html) · [추론 프로파일 리전](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html)
