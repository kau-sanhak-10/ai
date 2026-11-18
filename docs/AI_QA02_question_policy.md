# AI-QA-02 질문 횟수 점검

점검일: 2026-11-17. 관련 기능은 C-07·C-09, API 기준은 `api-명세서.md` 6.2~6.4다.
AI-QA-01과 함께 리뷰할 수 있는 AI 저장소의 보완 사항과 Spring 후속 작업을 구분한다.

## 항목별 횟수 계약

| 같은 항목에 적용된 질문 총수 | 요청의 `askCounts[field]` | 여전히 미해결인 경우 |
| --- | --- | --- |
| 0 | 키 생략 | 최초 질문 가능 |
| 1 | 0 | 보완 1회 가능 |
| 2 | 1 | 보완 2회 가능 |
| 3 | 2 | 추가 질문 중단, 필수·참고정보 기준 적용 |

- `ASK_MORE_INFO`와 `REQUEST_IMAGE`의 `targetFieldKeys`를 항목별로 합산한다. 방식이 바뀌어도
  초기화하지 않는다. 작업 유형 질문의 키는 기존 계약대로 `work_category`다.
- 동일 판단의 같은 대상 항목은 한 번만 센다. 질문 전의 키 생략과 최초 질문 후의 0을 구분한다.
- 질문 한도는 최대치다. 정보가 먼저 확인되면 남은 횟수를 채우지 않는다.
- 현재 기본 필수정보는 `symptom`이다. 예약 단계에는 작업 유형도 확인되어야 한다.
  업체별 규칙이 참고 항목을 조건부 필수로 지정했다면 해당 업무에서는 필수정보로 판단한다.
- 필수정보 미해결은 한도 후 이관한다. 참고정보만 미확인이면 미확인 상태로 남기고 진행하며,
  해당 이유만으로 사장님 확인 업무·알림을 만들지 않는다. 안전·직접 상담 우선 처리는 유지한다.

## 책임과 확인 범위

| 기준 | FastAPI·LangGraph | Spring |
| --- | --- | --- |
| 최초 질문 제외·문자/사진 합산 | 전달받은 0/1/2 기준으로 판단. 모의 저장소도 같은 의미 사용 | 적용된 질문을 DB에서 항목별 집계해 전달 |
| 실패·STALE·중복 결과 제외 | 분석 실패 시 오류 반환. 서버에 질문 횟수를 저장하지 않음 | 버전·응대 주체·중복 검사와 결과 저장을 원자적으로 수행하고 유효한 성공 질문만 집계 |
| 조기 확인 시 진행 | 최신 사실이 충분하면 질문을 중단하고 다음 행동 반환 | 최신 DB 스냅샷 전달, 확인된 결과 저장·재사용 |
| 한도 후 필수/참고 분리 | 필수 미해결 이관, 참고 미확인 유지·진행 | AI 결과에 맞게 문의 상태 적용. 참고 미확인만으로 별도 이관하지 않음 |

`SimulatedSpring`은 실제 Spring·DB가 아닌 메모리 모사다. 이 저장소의 테스트 통과는 실제
백엔드의 저장·복구·동시성·중복 방어를 검증한 결과가 아니다.

## AI 저장소에서 보완한 내용

- 사진 판독 안내가 질문 대상 없이 재촬영을 요청하던 경로를 수정했다. 고객 답변은 판독 상태만
  설명하고, 구조화된 `photoGuidance`는 보존한다. `CHECK_SCHEDULE` 중에 추가 촬영을 요구하거나,
  증상을 묻는 답변 뒤에 별도 모델명 재촬영 요청을 숨겨 덧붙이지 않는다.
- 사진 분석 완료 ID 수인 `processedPhotoIds`를 보완 질문 한도로 사용하지 않는다.
- `REQUEST_IMAGE`는 기존 응답 계약으로 유지한다. 실제 문자·사진 보완 요청은 질문 대상과 함께
  반환되어야 한다. 모의 저장소의 교차 방식 집계 테스트는 이 결과 적용 계약을 검사하며,
  현재 graph가 모든 사진 판독 실패에서 자동으로 `REQUEST_IMAGE`를 선택한다는 의미가 아니다.
- 요청 모델의 `askCounts` 설명에 키 생략/0/1/2와 집계 행동을 명시했다. JSON 필드·타입은 유지한다.
- `tests/test_ai_qa02_question_policy.py`에 횟수 경계, 필수/참고정보 분기, 항목별 교차 방식 집계,
  거부된 결과·실패 분석의 상태 보존, 조기 진행, 집계되지 않는 재촬영 요청 방지 검사를 추가했다.

## 백엔드에서 맞출 부분

2026-11-17 확인한 [backend develop의 AiAnalysisStore.java](https://github.com/kau-sanhak-10/backend/blob/develop/src/main/java/com/ilro/backend/domain/ai/service/AiAnalysisStore.java)
기준 `askCounts()`는 `counts.merge(key.asString(), 1, Integer::sum)`으로 최초 질문부터 1로 센다.
이 값은 AI의 보완 횟수 의미와 다르다. 확인한 파일의 Git blob SHA는
`1b71ea214b0821d4242e183de1b002a3a898d7de`이며, Dev 실행본과 같다는 의미는 아니다.

1. 성공 질문을 항목별로 집계할 때 최초 적용은 0, 이후 적용부터 +1로 전달한다.
   전체 질문 수를 내부에서 유지한다면 질문이 한 번 이상 있는 항목에 한해 총수에서 1을 뺀다.
   아직 질문하지 않은 항목을 임의로 0으로 채우지 않는다.
2. `ASK_MORE_INFO`·`REQUEST_IMAGE`를 함께 집계하고, 동일 판단 내 대상 키 중복은 제거한다.
3. 실패·STALE·중복 적용 기록은 횟수를 늘리지 않아야 한다. 조회한 코드는 성공 결과 필터와
   중복·버전 검사 경로가 있으므로 기존 방어를 유지하면서 저장소 통합 테스트로 확인한다.
4. 같은 문의의 최신 확인값을 다음 분석에 전달하고, 참고정보 미확인만으로 이관하지 않는다.

AI에서 임의로 입력 횟수에 1을 빼는 호환 처리는 추가하지 않는다. 올바른 보완 횟수를 보내는
백엔드나 로컬 모사에 이를 적용하면 질문 한도가 다시 어긋난다.

## Dev 합격 기준

- 같은 필수 항목이 미해결인 새 합성 문의에서 최초 질문 → 보완 1회 → 보완 2회 → 이관을 확인한다.
- 각 분석 요청의 항목 키 생략/0/1/2와 실제 저장된 질문을 대조한다. AI 호출 재시도 수와 구분한다.
- 중간에 확인된 답변을 보내면 추가 질문 없이 다음 단계로 진행하는지 확인한다.
- 문자·사진 보완을 같은 항목에서 합산하고, 중복·실패·STALE 결과가 질문을 늘리지 않는지 확인한다.
- 참고정보만 미확인인 사례는 진행하며, 판독 상태 안내가 별도 재촬영을 요청하지 않는지 확인한다.
- AI-QA-01의 정정 증상 저장·다음 턴 재사용도 같은 배포본에서 검증한다.

## 로컬 검증 기록과 함께 리뷰할 파일

2026-11-17 실제 실행: AI-QA-02 관련 테스트 **104개 통과**, AI-QA-01을 포함한 전체 테스트
**537개 통과**, 아래 Python 7개 파일의 Ruff 린트·포맷 검사와 `git diff --check` 통과.
Windows 임시 폴더 접근 제한은 이전 실행에서 확인돼, 전체 테스트는 권한이 허용된 환경에서
실행했다. 실제 Bedrock·Spring·Dev에는 연결하지 않았다.

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_ai_qa02_question_policy.py tests/test_ai02_conversation.py tests/test_ilro_multiturn.py tests/test_photo_vision.py tests/test_reference_conflict_policy.py -p no:cacheprovider --tb=short
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --tb=short
.\.venv\Scripts\ruff.exe check app/intake/graph.py app/intake/contracts.py app/intake/symptom_rules.py tests/test_ai_qa02_question_policy.py tests/test_photo_vision.py tests/test_ai02_conversation.py tests/test_intake_pr_feedback.py
.\.venv\Scripts\ruff.exe format --check app/intake/graph.py app/intake/contracts.py app/intake/symptom_rules.py tests/test_ai_qa02_question_policy.py tests/test_photo_vision.py tests/test_ai02_conversation.py tests/test_intake_pr_feedback.py
git diff --check
```

AI-QA-01·02를 묶어 리뷰할 파일은 다음 10개다. 커밋·push·병합·배포는 이번 작업에서 수행하지 않았다.
기존 변경이 함께 있는 파일은 diff를 검토하고, 다른 AI-01 작업·환경설정은 이번 묶음과 구분한다.

```text
app/intake/symptom_rules.py
app/intake/graph.py
app/intake/contracts.py
tests/test_intake_pr_feedback.py
tests/test_ai02_conversation.py
tests/test_photo_vision.py
tests/test_ai_qa02_question_policy.py
docs/AI02_conversation.md
docs/photo_intake.md
docs/AI_QA02_question_policy.md
```
