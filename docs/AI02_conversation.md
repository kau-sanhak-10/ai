# AI-02 LLM API 대화·정보 추출·추가 질문

작성일: 2026-11-06 · 상태 갱신일: 2026-11-07. 이 문서는 AI-02 구현·검증 결과를 정리하며 이전 DS-05 검증 기록과 구분한다.

## 현재 완료 상태

**AI 서버의 핵심 구현과 Gemini 기본 사례 검증을 마쳤으며, 실제 Spring 문의 저장 API 연결·왕복 검증이 남아 있다. WBS 전체 완료로 표시하지 않는다.**

| WBS 완료 기준 | 현재 상태 | 확인 근거·남은 작업 |
| --- | --- | --- |
| ① 증상·요청·확인/부족 항목 구조화, 모르는 값은 미확인 유지 | 구현·기본 사례 검증 완료 | 구조화 출력·미확인 처리 자동 테스트, 기본 3턴 Gemini 사용자 실행 기록 |
| ② 최신 상태·이전 대화를 사용한 문맥·부정·정정 해석 | 구현·고정 사례 실호출 확인 | 부정 1턴·정정 2턴에서 실제 추출 후보와 최종 상태 대조 |
| ③ 반복 질문 방지·OWNER 값 보호·추출/분류 통합 호출 | 구현·기본 사례 검증 완료 | 보완 횟수·재질문 방지 자동 테스트, 기본 대화와 OWNER 제조사 충돌 실호출 |
| ④ 실제 문의 저장 연결·기본 사례 실행·호출 메타데이터 기록 | 부분 완료 | 기록 기능과 SDK 대역의 성공·실패·시간 초과 검증 완료. 실제 Spring 호출·DB 저장·후속 요청의 상태 재사용 검증 필요 |

아래 Gemini 실험은 로컬 시제품의 고정 사례 검증이다. 현재 FastAPI의 Bedrock 경로·실제 Spring DB 저장 검증과는 별개다.

## 기준과 범위

- 분석 API와 LangGraph가 이번 저장소의 범위다. 문의 저장·AI 작업 재시도·최신 버전 검사·상태 변경은 Spring BE-05/08의 책임이다. FastAPI에 별도 문의 DB나 예약 호출을 추가하지 않았다.
- AI-02 작업은 기존 LangGraph의 8개 노드·Edge 연결을 유지했다. 문맥 전달·정정 병합·보완 횟수에 따른 판단은 노드 내부 로직과 분기 조건에서 보완했다.

## 구현 내용과 실행 흐름

1. Spring이 저장한 `fields`, `askCounts`, `messages`를 받아 최신 상태를 모델 입력 JSON으로 만든다. 전달받은 대화를 청킹하거나 개별 문장으로 나누지 않는다. AI/OWNER 메시지도 이전 질문의 문맥으로 전달한다.
2. 문자 추출과 의도·작업 유형 분류를 한 번의 모델 요청에서 서로 다른 필드로 받는다. 사진 분석은 기존 별도 경로를 유지한다.
3. 추출한 `messageId`·`observedText`를 고객 메시지에 대조한다. AI/OWNER 문장이나 없는 메시지 ID는 고객 사실의 근거로 사용할 수 없다. 신규 메시지 ID가 생략된 기존 출력은 고객 원문을 역순으로 찾아 호환한다.
4. `잘못 말했어요`·`아니라` 등의 명시적인 정정은 모델의 `isCorrection` 표시, 최신 메시지 ID, 실제 원문을 함께 검사한다. 고객의 이전 설명만 대체하며 사진/OWNER 값과 다르면 기존 충돌 정책을 유지한다. 정정 없는 다른 증상은 자동 교체하지 않고 재확인한다.
5. 정정값이 저장된 뒤에는 그 값보다 과거 메시지에서 재추출된 후보를 무시한다. `모르겠어요`는 `CUSTOMER_UNSURE`로 남기며, 명시적으로 기존 설명을 철회하면 예전 확정값으로 예약 단계에 진입하지 않는다.
6. 규칙으로 필요한 항목 하나를 선택한다. 이미 확인한 항목과 필수가 아닌 미확인 참고정보는 반복 질문하지 않는다. 최초 질문 뒤 보완은 같은 항목에서 최대 2회이며 문자/사진 방식이 바뀌어도 이어서 센다. 필수정보 미해결은 이관, 참고정보만 미확인이면 상담을 계속한다. 안전·직접 상담 요청은 기존 우선 이관을 유지한다.
7. 답변과 검증 결과를 기존 `ApiResponse`로 반환한다. 모델·형식 오류 시 내부 재시도를 하지 않고 명세 오류를 반환한다. Spring이 한 작업의 전체 호출 횟수·대기 예산을 관리한다.

`isCorrection`, `unsure`, 모델 출력의 `messageId`는 내부 추출 스키마 속성이다. Spring에 전달하는 `facts`에 새 속성을 추가한 것은 아니다. 프롬프트 버전은 `intake-ai02-v2`다.

## 수정한 파일

| 파일 | 변경 목적 |
| --- | --- |
| `app/intake/context.py` | 공통 문맥 JSON·프롬프트 버전·명시적 정정 표현 검사 |
| `app/intake/providers.py` | 동기/비동기 문맥 추출, 고객 원문 검증, 미확인·정정 후보, 내부 재시도 제거·사용량 보존 |
| `app/intake/contracts.py` | 빈 메시지 거부, 보완 횟수 의미 명시, 내부 후보에 정정 표시 추가 |
| `app/intake/intent_rules.py`, `app/intake/symptom_rules.py` | 함께 유지하는 안정성 보완: 직접 연결 의도 근거 검증·모호한 증상 판별 |
| `app/intake/graph.py` | 문맥 추출 연결, 정정/철회 반영, 과거 값 재등장 방지, 보완 2회 상한 |
| `app/intake/audit.py`, `app/api/inquiries.py`, `app/main.py` | 성공/실패/전체 시간 초과에 대한 JSON 메타데이터 로그 |
| `app/core/config.py`, `.env.example` | 함께 유지하는 안정성 보완: 전체 분석 제한 시간 `INTAKE_TIMEOUT_SECONDS` 설정 |
| `try_my_intake_gemini.py` | 기존 Gemini 시제품에도 같은 문맥·정정 후보·무재시도 규칙 적용 |
| `check_ai02_live.py`, `tests/test_ai02_live_checks.py` | 부정·정정·OWNER 초기값의 재현 가능한 실호출 사례와 잘못된 성공 판정 방지 검사 |
| `simulate_ilro_multiturn.py` | Spring 모사의 보완 횟수 계산과 문자/사진 통합 카운터 수정 |
| `tests/test_ai02_conversation.py` | 실제 API 경로의 SDK 대역 검증, 정정·철회·출처 보호·질문 상한·로그 회귀 검증 |
| 기존 provider/deadline/multiturn 테스트 | 재시도 제거와 보완 횟수 의미를 최신 명세에 맞춤 |
| `eval/08_ds05_mock_fixtures_v1_1.jsonl` | DEV-014의 보완 2회 소진 후 기대 결과를 추가 질문에서 이관으로 수정 |
| `README.md`, `DS05_intake_graph.md`, 이 문서 | 실행 방법·기존 설계 설명을 실제 구현과 일치시킴 |

작업 시작 전에 존재하던 다른 미커밋 수정과 미추적 파일은 유지했다. 기능 명세 CSV와 API 원본 문서는 수정하지 않았다.

## API 계약과 확인이 필요한 차이

- 경로·헤더·공통 응답·필드명·오류 코드는 기존 서비스 계약을 유지했다. 분석 결과의 `requestId`·`inputVersion`도 그대로 반환한다.
- `askCounts`를 명세 6.2의 **보완 횟수**로 교정했다. 최초 질문 뒤 `0`, 첫 보완 뒤 `1`, 두 번째 보완 뒤 `2`다. 기존 코드처럼 질문 합계를 보내면 한도를 조기에 소진하므로 Spring의 집계도 같은 기준이어야 한다.
- 3.8 오류 표의 “다시 요청 1회 후에도”와 6.4의 “추가 모델 호출 없이 오류 반환·재시도는 Spring 하나로 통일”은 문서 내에서 다르다. 구체적인 책임·시간 예산이 정의된 **6.4를 적용**했다. 원본 문구 정합성은 담당자 확인이 필요하다.
- 현재 FastAPI는 Bedrock 텍스트 경로를 사용한다. 실제 모델 ID·리전·실행 권한은 Dev에서 확인한다. OpenAI/Gemini 직접 API는 기존 실험으로만 보존하고 운영 경로로 자동 전환하지 않는다.
- 기존 `intentEvidence`·`processedPhotoIds`·`analyzedPhotoIds` 확장과 `work_category` 질문 키는 유지했다. 이 항목의 Spring DTO 수용·처리 이력 저장은 별도 합의 대상이다.
- 이름·전화번호·주소를 구조화된 접수 필드로 추가하지 않았다. 지역·인증은 Spring이 검사한다. **자유 대화에 고객이 직접 적은 개인정보를 찾아 제거하는 기능은 아니다.** Spring에서 모델에 전달할 대화의 개인정보 최소화·비식별 처리가 필요하다.

## 운영 기록

`app.intake.audit` 로거가 분석마다 `event=intake_analysis` JSON을 INFO 수준으로 출력한다.

- 기록: `requestId`, `inputVersion`, `modelName`, `promptVersion`, `ruleVersion`, `latencyMs`, `inputTokens`, `outputTokens`, `modelCalls`, `usageReportedCalls`, `status`, `errorType`.
- 제외: 대화·사진·URL·주소·키·예외 메시지·LLM 원시 출력. 별도 문의 데이터 저장소를 만들지 않는다.
- 응답을 받은 뒤 형식 검증에 실패해도 반환된 토큰은 합산한다. SDK 내부 파싱 예외·네트워크 오류·취소로 사용량을 받지 못하면 `usageReportedCalls < modelCalls`일 수 있다. 기록된 0토큰은 무료 처리나 청구 0을 뜻하지 않는다.
- 실제 로그 보관·접근권한·보존기간은 배포 환경의 로그 수집 설정에서 관리해야 한다. CLI의 `--debug-extraction`은 원문이 포함되는 개발용 출력이므로 운영 로그와 별개다.

## 검증 결과와 재현

다음은 2026-11-06에 실제 실행한 단계별 기록이다. **258개는 실호출 판정기 테스트를 추가하기 전의 전체 실행**, 29개는 추가 후 관련 파일만 실행한 결과다. 두 실행에는 중복된 테스트가 있으므로 통과 수를 합산하지 않는다.

| 실행 단계 | 검증 범위 | 결과 |
| --- | --- | --- |
| AI-02 수정 전 | 당시 전체 테스트 | 234개 통과 |
| AI-02 핵심 구현 후 | 당시 전체 테스트, Ruff 린트·포맷 검사(55개 파일), `git diff --check` | 258개 통과, 검사 통과 |
| 실호출 도구·판정기 테스트 추가 후 | `test_ai02_live_checks.py`·`test_ai02_conversation.py` 부분 실행, Ruff 린트·포맷 검사(57개 파일), `git diff --check` | 29개 통과, 검사 통과 |

3턴 모의 대화는 `ASK_MORE_INFO → ASK_MORE_INFO → CHECK_SCHEDULE`과 STALE 폐기를 확인하여 PASS였다. 위 자동 테스트는 SDK 대역과 Spring 메모리 모사의 검증이며 실제 모델 품질이나 Spring DB 저장 성공을 뜻하지 않는다. Gemini 실호출은 아래에 별도로 기록했다.

2026-11-07 문서 갱신에서는 코드·명세·저장된 실험 JSON을 대조했다. 전체 테스트와 외부 API를 다시 실행하지 않았으며, 이번 갱신으로 새로운 실호출 성공이나 전체 테스트 통과를 추가 선언하지 않는다.

실제로 실행한 명령:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --tb=short
.\.venv\Scripts\python.exe -m ruff check --no-cache
.\.venv\Scripts\python.exe -m ruff format --check --no-cache
.\.venv\Scripts\python.exe -X utf8 simulate_ilro_multiturn.py --mode mock
git diff --check
```

프로젝트 표준 환경에서는 다음과 같이 실행한다.

```powershell
uv run pytest
uv run ruff check
uv run ruff format --check
uv run python simulate_ilro_multiturn.py --mode mock

# 실제 호출·사용료 발생. GEMINI_API_KEY 필요
uv run python simulate_ilro_multiturn.py --mode gemini --turn "보일러가 안 돼요" --turn "온수가 안 나와요" --turn "수리를 받고 싶어요"

# 서비스 경로: INTERNAL_KEY, BEDROCK_MODEL_ID, AWS 리전·실행 권한 필요
uv run uvicorn app.main:app --reload --port 8000
```

최초 Gemini 실호출 시도는 기존 설정 모델 `gemini-3.5-flash-lite`에 비식별 내장 3턴 사례를 사용했다. 첫 요청에서 제공사 **HTTP 503 ServerError**를 받아 종료했고 내부 재호출을 하지 않았다. 이후 사용자가 같은 모델의 기본 3턴 성공 로그를 제공했다. 로그에서 증상 미확인 → 온수 증상 확정 → `CHECK_SCHEDULE` 및 STALE 폐기를 확인했다. 이 기록은 사용자가 제공한 실행 결과이며, 아래 추가 실험은 에이전트가 직접 실행한 별도 결과다. OpenAI 서비스 모델의 실호출은 여전히 검증하지 않았다.

## 부정·정정·사장님 값 보호 실호출 (2026-11-06)

관련 기능은 C-04·C-07·C-08 및 API 명세 6장의 OWNER 보호·원문 근거 규약이다. `check_ai02_live.py`는 기존 `SimulatedSpring`과 `execute_turn`을 재사용한다. **이번 실험 준비에서는 서비스 코드·LangGraph 노드/Edge·API 계약을 변경하지 않았다.**

비식별 고정 입력으로 `gemini-3.5-flash-lite`를 실제 4회 호출했다. 세 사례 모두 자동 검사 PASS였고, 저장된 후보·근거·충돌·최종값을 직접 대조했다.

| 사례 | 입력·초기 상태 | 실제 결과 |
| --- | --- | --- |
| 부정 표현, 1회 | “온수는 잘 나오는데 난방이 안 돼요. 수리받고 싶어요.” | 정상 온수와 난방 문제를 구분한 원문을 `CONFIRMED`로 저장, `CHECK_SCHEDULE` |
| 고객 정정, 2회 | 먼저 “난방이 안 돼요.”를 저장한 뒤 “제가 잘못 말했어요. 난방은 정상이고 온수가 안 나와요. 수리받고 싶어요.” | `isCorrection=true`, `messageId=903`의 새 원문 근거로 “온수가 안 나와요”를 저장. 증상 충돌·재질문 없이 `CHECK_SCHEDULE` |
| 사장님 값 보호, 1회 | `manufacturer=경동나비엔`, `source=OWNER`를 초기값으로 설정하고 고객은 “제가 잘못 알았어요. 귀뚜라미 보일러인데 온수가 안 나와요. 수리받고 싶어요.” | 고객 후보 `귀뚜라미`를 실제 추출했지만 제조사 변경 fact는 없음. OWNER 경동나비엔 유지, 양쪽 출처의 충돌 표시 후 제조사 재확인 |

호출별 측정값:

| 호출 | 입력 토큰 | 출력 토큰 | 실험 경과(ms) |
| --- | ---: | ---: | ---: |
| 부정 표현 | 697 | 115 | 1762 |
| 정정 전 | 685 | 98 | 1035 |
| 정정 후 | 822 | 110 | 1357 |
| OWNER 값과 고객 설명 충돌 | 738 | 158 | 1503 |

토큰은 기존 어댑터가 SDK에서 수집한 값이며 청구액을 의미하지 않는다. `elapsedMs`는 그래프·클라이언트 준비를 포함해 실험 도구에서 잰 경과 시간이다. 동기 CLI의 기존 `usage.latencyMs=0`을 실측값으로 해석하지 않는다.

상세 입력·모델 후보·노드 결과는 로컬 `results/ai02_live_cases.json`에 저장했다. `git check-ignore`로 Git 제외를 확인했으며, 비식별 고정 사례만 담긴 실험 기록이다. 추가 검증 명령도 실제 실행했다.

```powershell
.\.venv\Scripts\python.exe -X utf8 check_ai02_live.py --case all --save results/ai02_live_cases.json
.\.venv\Scripts\python.exe -m pytest -q tests/test_ai02_live_checks.py tests/test_ai02_conversation.py -p no:cacheprovider --tb=short
```

실호출 3개 사례 PASS, 관련 비용 없는 테스트 **29개 통과**. 이 29개 실행에는 외부 호출이 포함되지 않는다. 판정기 테스트는 정상/고장 대상이 뒤바뀐 출력, 모델이 충돌 후보를 누락한 출력, DB 방어만으로 값이 유지됐지만 AI는 덮어쓰기를 제안한 출력을 성공으로 처리하지 않는지 확인한다. 호출 오류는 재시도 없이 중단하며 예외 원문을 출력하지 않는다.

사용자 터미널에서 같은 실험을 재현하려면:

```powershell
# 전체 3사례, 실제 API 최대 4회 호출·과금 발생
uv run python -X utf8 check_ai02_live.py --case all --save results/ai02_live_cases.json

# 각각 선택 실행
uv run python -X utf8 check_ai02_live.py --case negation
uv run python -X utf8 check_ai02_live.py --case correction
uv run python -X utf8 check_ai02_live.py --case owner
```

고정 표현을 확인하는 유한한 검사가 포함돼 있으므로 표현이 달라 실패하면 JSON 후보와 저장값을 함께 검토해야 한다. 세 사례의 성공은 다양한 부정·정정 표현 전체의 정확성을 보장하지 않는다. 실제 Gemini 추출·LangGraph 병합·메모리 상태 적용은 확인했지만 **실제 Spring DB 저장·FastAPI의 OpenAI 제공사 실호출을 검증한 것은 아니다.**

OpenAI SDK의 비동기 호출·재시도 제어는 [공식 Python SDK 문서](https://github.com/openai/openai-python#retries), Pydantic 구조화 출력은 [공식 가이드](https://developers.openai.com/api/docs/guides/structured-outputs)를 참조했다.

## WBS 완료를 위해 남은 연동 검증

Spring BE-05/08 담당자와 다음 항목을 실제 환경에서 확인해야 한다.

- [ ] Dev에서 승인된 Bedrock 텍스트 모델로 Spring → FastAPI → Bedrock 성공 응답을 확인한다.
- [ ] Spring이 DB의 최신 `fields`·출처·대화·`askCounts`·`safetyFlag`·`inputVersion`을 전달하는지 확인한다. `askCounts`는 최초 질문을 제외한 보완 횟수로 집계한다.
- [ ] 분석 결과·접수 항목·AI 답변·처리 기록을 실제 DB에 함께 저장하고, 다음 요청과 재시작 후 복원에서 저장 상태를 재사용하는지 확인한다.
- [ ] 결과 적용 시 문의 잠금·트랜잭션으로 최신 입력 버전·AI 응대 여부·중복 요청을 확인한다. STALE·인계 이후·중복 응답은 적용하지 않고 `OWNER` 값을 보호한다.
- [ ] 실제 실패·시간 초과에서 Spring이 같은 입력을 최초 포함 최대 2회·전체 대기 예산 60초 안에서 관리하며, FastAPI/SDK가 자동 재시도하지 않는지 확인한다. 권한·크레딧·일일 할당량 오류는 재시도하지 않는다.
- [ ] 실제 연동의 성공·실패·시간 초과에서 모델/프롬프트 버전·사용량·처리시간·실패 분류가 기록되는지 확인한다.
- [ ] 기존 확장 필드 `intentEvidence`·`processedPhotoIds`·`analyzedPhotoIds` 및 `work_category` 질문 키의 Spring DTO 수용·저장 정책을 확인한다.

## 후속 품질 확인과 별도 작업

- 기본 3턴의 사용자 실행 기록과 부정·정정·OWNER 보호 고정 사례는 확인했다. 짧은 답변·다중 증상·다양한 동의 표현의 평가 사례를 늘린다. 이번 사례만으로 의미 정확성 전체를 보장하지 않는다.
- 업체별 세부 질문·필수 항목, 선택 작업 유형의 수정 우선순위는 업체·백엔드 담당자와 확인한다.
- YOLO·OCR 사진 연결, 매뉴얼 검색, 예약 확정·알림 생성은 이 문서의 텍스트 상담 검증과 별개다.
