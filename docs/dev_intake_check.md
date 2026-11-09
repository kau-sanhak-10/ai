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
4. 사진 실연은 승인된 6클래스 YOLO 가중치와 OCR을 연결한 뒤, 실제 허용된 S3 임시 URL로 사진을 읽어 사진 ID가 있는 추출값 또는 ‘미확인’을 Spring이 저장·조회하는 것까지 확인하고 켠다. 연결 전에는 텍스트만 실연하고 사진 판독은 별도 실험 결과로 보여준다.

[Converse 권한](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html) · [추론 프로파일 리전](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html)
