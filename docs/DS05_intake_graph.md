# DS-05 · AI 입출력 / LangGraph 처리 흐름 (WBS 범위)

갱신일: 2026-11-06. 기준: `api-명세서_version2.txt` 6장 및 10/26 확정 운영 기준.
구현 대조 기준: LangGraph 및 문의 판단 PR을 바탕으로 Bedrock 텍스트 경로를 반영했다. 실제 Dev 호출과 YOLO·OCR 사진 연결은 별도 검증한다.

## 담당 시스템과 범위

- 원민재: FastAPI `POST /internal/v1/inquiries/analyze`, LangGraph State·8개 Node·조건부 Edge, 공통 응답/출력 검증.
- 정채우: Spring 비동기 호출, DB에 있는 최신 문의 상태의 구성, `ai_input_seq`/`handler_mode`/`OWNER` 출처 검증, 상태·이력·알림·예약 저장.
- 박준형: 내부 접속·권한·배포 지원.
- 프론트: Spring의 방문 시간 조회, 고객 선택 및 최종 확인, 예약 확정 API 호출.

**AI에는 예약/이관 상태를 수정하는 도구와 DB 연결이 없다.** `CHECK_SCHEDULE`은 화면 진입 행동 **제안**일 뿐 최종 예약 가능 판정이 아니다. `handlerMode=OWNER` 상태로 새로 AI를 호출하지 말아야 하고, 중간에 OWNER로 바뀐 결과는 Spring이 버전과 응대 주체 확인 뒤 폐기한다.

이 저장소에 구현된 범위는 FastAPI 분석 요청 수신부터 LangGraph 결과 반환까지다. Spring DB 저장·재시작 복원·비동기 재시도·예약 확정의 실제 구현 및 왕복 검증은 이 저장소의 모의 테스트로 완료 판정하지 않는다.

2026-11-06 AI-02에서 최신 문맥 전달·고객 정정·보완 횟수·무재시도·운영 기록을 보완했다. 최종 258개 테스트 및 Ruff 검사를 통과했으며, 이번 변경과 실호출의 한계는 [AI02_conversation.md](AI02_conversation.md)를 기준으로 확인한다. 아래 기존 실호출·테스트 수치는 당시 검증 기록이다.

후속 Gemini 실호출에서는 부정 표현 1턴·고객 정정 2턴·OWNER 제조사 보호 1턴을 실행해 세 사례 모두 PASS를 확인했다. `check_ai02_live.py --case all`로 재현하며 상세 결과는 위 AI-02 문서의 실험 기록을 참조한다. Spring 저장은 메모리 모사이며 실제 DB 검증과 구분한다.

## API

- 경로: `POST /internal/v1/inquiries/analyze`, 헤더: `X-Internal-Key` (Spring과 공유하는 비밀키).
- 요청: 명세 6.2 `requestId`, `inquiryId`, `ownerId`, `inputVersion`, `handlerMode`, `business`, `fields`, `askCounts`, `messages`, `photos`, `safetyFlag`.
- 사진 처리 이력용 선택 필드 `processedPhotoIds`를 추가했다. 기본값은 `[]`이며 기존 요청도 유효하다. 응답에는 이번 요청에서 성공적으로 분석한 `analyzedPhotoIds`와 검증된 연결 의도 근거 `intentEvidence`를 추가했다. Spring DTO 및 저장 정책과 함께 연동해야 한다.
- 응답: `ApiResponse[AnalyzeResult]` — `{success,code,message,result}`. 결과는 명세 6.3의 `facts`, `conflicts`, `interpretations`, `workCategory`, `safety`, `intent`, `nextAction`, `reply` 및 버전·모델 사용량 메타데이터.
- 현재 계약의 `FieldKey`는 `manufacturer`, `model`, `boiler_type`, `error_code`, `symptom`이다. 구조화된 고객 이름·주소·전화번호·예약 슬롯 필드는 없다. 연락처 인증·방문 주소·지역·일정 조건은 Spring이 관리·검증한다. 대화에 개인정보가 포함될 수 있으므로 구조화 필드 부재만으로 외부 전송 데이터가 비식별이라고 보장할 수는 없다.
- 문의/업체·사진 ID의 **소유권 확인은 Spring의 책임**이다. AI는 공유 내부 키를 인증하고, 반환된 사실·충돌 후보의 근거 ID가 요청의 메시지·사진 또는 기존 필드 근거에 포함되는지 검사한다. 이는 실제 DB 소유권 조회를 대신하지 않는다. 사진 URL은 HTTPS 및 일부 로컬·사설 주소 차단 규칙을 적용한다.
- 데이터 원본은 Spring DB다. `IntakeState.request` 안에 `inquiryId`, `ownerId`, `inputVersion`, `handlerMode`가 연결되고 실행 단계는 `stage=EXTRACT/DECIDE/SUMMARY`로 기록된다. Checkpointer나 AI 측 문의 저장소는 사용하지 않는다.
- 재시작 후에는 Spring이 DB에서 복원한 `fields`, `askCounts`, `messages`, `safetyFlag`, 최신 입력 버전·응대 주체를 새 요청으로 보내야 한다. 실행 중의 `stage`는 임시 상태이며 이 저장소에서 DB에 저장하지 않는다. Spring은 결과 적용 시 최신 `inputVersion`·응대 주체·중복 요청을 다시 검사해야 한다.

### 항목·근거·해석의 구분

- `RawObservation`: 모델 추출 후보. `value`, `observedText`, `source`, `evidence`, `unconfirmedReason`을 분리한다. 문자 근거는 실제 고객 발화에 포함되는지 어댑터에서 확인한다.
- `facts`: 서버 현재값과 대조한 항목별 값·출처·확인 상태·미확인 이유. AI의 `source`는 `CUSTOMER/PHOTO`이며 기존 `OWNER` 값은 덮어쓰지 않는다. `보일러가 고장 났어요`, `보일러가 이상해요`처럼 구체적인 부위·상태가 없는 증상은 원문 값을 보존하고 처음부터 `status=UNCONFIRMED`, `unconfirmedReason=NOT_PROVIDED`로 반환한다. 여기서 `NOT_PROVIDED`는 구체적인 핵심 증상이 제공되지 않았다는 뜻이다.
- `conflicts`: 기존 값과 새 후보를 근거와 함께 반환한다. 충돌한 값으로 제품별 안내나 필수정보 충족을 확정하지 않는다.
- `interpretations`: 사진에서 직접 읽은 값과 구별되는 매뉴얼 해석. 제조사·모델이 일치하는 검색 자료만 사용하고 출처를 반환한다. 오류코드만으로 고장 부품이나 견적을 확정하지 않는다.

## Graph: 8개 Node

```text
START → extract_message → (photos?) analyze_image → merge_facts
      → check_rules → (강제 처리?) generate_reply
                    → decide_action → (RAG?) search_manual
                                       → generate_reply
      → validate_result → END
```

| Node | 읽는 State | 작성 State | 실패 처리 |
| --- | --- | --- | --- |
| extract_message | 최신 fields, askCounts, 이전 질문을 포함한 messages, safetyFlag | observations, intent, workCategory, safety, stage=EXTRACT | 출력 검증 실패 시 재호출 없이 오류 반환 |
| analyze_image | photos, processedPhotoIds, safety | observations(누적), safety, analyzedPhotoIds | 처리 이력의 사진은 제외, 문자 판독 불가는 미확인 후보, 호출 실패는 오류 |
| merge_facts | fields, observations, workCategory, askCounts, rules | facts, conflicts | 같은 근거 재추출 제외, OWNER 값 보호, 필수/참고 충돌 구분 |
| check_rules | intent, safety, handlerMode | 강제 nextAction 또는 null, stage=DECIDE | OWNER 모드 거부, 안전·명시적 연결/회신 요청 우선 이관 |
| decide_action | 원본 및 확인 정보, askCounts, workCategory, business | nextAction | 질문 한도·안전·예약 단계 규칙 검사 |
| search_manual | 이전 messages, 모델·제조사 | manualHits | 확인한 해당 제품의 자료만 허용; 자료 없으면 빈 검색 |
| generate_reply | nextAction, manualHits | reply, interpretations, stage=SUMMARY | 예약·이관 완료를 허위로 확정하지 않음 |
| validate_result | facts, conflicts, nextAction, reply, 사진/메시지 근거 ID | result | 출력 불일치 502, Spring이 최신 inputVersion 재검증 |

- Node State는 원본 입력 + 후속 단계에 필요한 **구조화된 Raw 값**만 보관한다. 사진 존재 여부·질문 횟수 합계·예약 슬롯 같은 파생값은 보관하지 않는다.
- `askCounts[field]`: 명세 6.2에 따라 **최초 질문을 제외한 보완 요청 횟수**다. 최초 질문 후 `0`, 보완 1회 후 `1`, 보완 2회 후 `2`이며 `2`이면 추가 질문을 멈춘다. 문자·재촬영 방식이 바뀌어도 같은 항목에서 합산한다. 작업 유형 질문은 `targetFieldKeys=["work_category"]`로 표시한다. 기존의 질문 합계 해석은 AI-02에서 바로잡았으며, Spring도 보완 횟수를 보내야 한다.
- 현재값과 **출처 및 messageId/photoId가 같은** 재추출 후보는 새 정보로 충돌 비교하지 않는다. 증상의 공백·문장 끝 구두점 차이는 정규화해 비교한다. 제조사·모델·오류코드의 식별 문자는 유지한다.
- 증상 구체성은 [symptom_rules.py](../app/intake/symptom_rules.py)에서 대상과 관찰 상태의 조합으로 판단한다. `온수 관련 문의예요`, `전원이요`처럼 대상만 언급하면 미확인이고, `온수가 안 나와요`, `따뜻한 물이 안 나와요`, `전원이 안 들어와요`처럼 상태가 함께 있으면 확인 후보로 취급한다. 물샘·가스 냄새 등 구체적인 현상도 별도 규칙으로 확인한다. 원인이나 고장 부품을 추론하지 않는다.
- 미확인 증상은 확인된 현재값 및 충돌 후보에서 제외하고 구체적인 보완 답변으로 갱신한다. 과거에 일반 고객의 모호한 증상이 `CONFIRMED`로 저장된 경우도 보완 답변과 충돌시키지 않는다. 기존 `OWNER` 값은 이 처리로 덮어쓰지 않는다. 서로 다른 구체적인 증상이 제시되면 기존대로 충돌 재확인을 진행한다.
- 모델은 연결·회신 의도와 함께 `intentEvidence={messageId, observedText}`를 반환한다. 최신 고객 메시지 ID와 원문 문구의 일치뿐 아니라 **그 근거가 포함된 절이 실제 연결 요청인지** 확인한다. 거절·부정·과거 통화 설명·인용된 안내 문구는 근거가 일치해도 이관 의도로 인정하지 않는다. `전화 좀 주실 수 있나요`, `사장님 바꿔주세요`, `사장님과 직접 이야기하고 싶어요`는 명시 요청으로 처리한다.
- 검증을 통과하면 모델이 선택한 `ASK_OWNER/CALLBACK`을 유지한다. 근거가 없거나 검증에 실패한 경우에도 별도의 명시 요청 절이 있으면 보완 규칙으로 확인한다. 이 보완 경로에도 같은 부정·과거 설명 검사를 적용한다. 확인되지 않은 모델의 연결 의도는 관찰값 유무에 따라 `ANSWER/OTHER`로 보정하고 거부된 `intentEvidence`는 응답에 남기지 않는다. 안전 플래그는 의도 보정과 관계없이 우선 처리한다.
- 필수 항목은 [rules/inquiry_types.yaml](../rules/inquiry_types.yaml)(YAML 1.2 호환 JSON 문법)에서 관리한다. 현재 공통 필수는 `symptom`이며 업무별 추가 필수 항목은 비어 있다. 참고 항목은 `manufacturer/model/boiler_type/error_code`다. 업체별 필수 항목의 개별 설정은 미구현이며, 요청의 `business`는 허용 작업 유형과 자동 예약 허용 여부를 전달한다.
- `decidedBy=RULE`: 현재 행동 선정 함수는 검증 규칙 기반이고, 외부 LLM은 대화/사진의 원시 관찰값과 의도/작업 유형을 구조화하는 역할이다.

### 미확인·충돌·질문 종료 정책

| 상태 | 현재 처리 |
| --- | --- |
| 안전 우려 또는 명시적 사람 연결·회신 요청 | 횟수를 기다리지 않고 `ESCALATE_TO_OWNER` 제안 |
| 필수정보 미확인·충돌, 또는 참고정보로 분류되지 않은 항목의 충돌 | 질문 한도 미만이면 재확인, 한도 도달 시 `ESCALATE_TO_OWNER` |
| 참고정보만 미확인 | 이 사유로 추가 확인 업무·알림·이관·예약 차단을 생성하지 않음 |
| 참고정보 충돌 | 한도 미만이면 재확인, 한도 도달 시 충돌을 유지하고 미확인 제안 후 다른 필수 조건 판단을 계속함 |
| 작업 유형 미확인 | 고객의 직접 선택을 강제하지 않고 대화로 질문. 한도 도달 후에도 미확인이면 이관 |
| 증상·작업 유형 확인, 업체 허용 유형 및 자동 예약 설정 충족 | `CHECK_SCHEDULE` 제안. 최종 예약 조건은 Spring에서 검증 |

참고 항목이라도 해당 작업의 필수 항목으로 설정되면 필수정보 정책이 우선한다. 참고정보 충돌의 미확인 제안은 `value=null`, `status=UNCONFIRMED`, `unconfirmedReason=CONFLICT`이며 기존 `OWNER` 값은 변경 제안하지 않는다. 해당 충돌만으로 사장님 확인 업무를 만들지 않는다.

### CHECK_SCHEDULE 이후의 책임 경계

1. `decide_action`이 `CHECK_SCHEDULE`을 선택한다.
2. `generate_reply`가 예약 화면 이동을 위한 안내 문구를 생성한다.
3. `validate_result`가 결과를 검증한다.
4. `END`로 그래프를 종료하고 FastAPI가 Spring에 결과를 반환한다.
5. Spring이 최신 입력 버전·응대 주체·인증·업체 권한·작업 유형·필수정보·안전·지역·일정 조건을 검사한다.
6. 프론트가 Spring의 방문 가능시간을 조회해 고객에게 표시한다.
7. 고객이 시간 선택 및 최종 확인을 한다.
8. 프론트가 Spring 예약 확정 API를 호출하고 Spring이 최신 일정·중복 요청 등을 최종 검증해 저장한다.

1~4단계는 이 저장소의 구현 범위이고 5~8단계는 후속 Spring·프론트 연동 범위다. 프론트는 안내 문구 대신 `nextAction.action`을 기준으로 처리해야 한다. MVP 작업시간 기본값 60분, 사장님 설정 유지, 이동·여유시간 반영도 Spring 예약 계산의 책임이며 AI가 계산하지 않는다.

## 모델·외부 모듈 경계

- **FastAPI 서비스 경로:** [app/api/inquiries.py](../app/api/inquiries.py)가 [BedrockExtractionProvider](../app/intake/bedrock_provider.py)를 생성한다. `INTERNAL_KEY`, `BEDROCK_MODEL_ID`, 실행 리전과 AWS 권한이 필요하다. 모델 설정·권한이 없으면 502 `AI_MODEL_ERROR`를 반환하며 OpenAI/Gemini 실험 코드로 대체하지 않는다.
- **로컬 Gemini 검증 경로:** [try_my_intake_gemini.py](../try_my_intake_gemini.py)의 `GeminiGraphTestProvider`를 사용한다. `GEMINI_API_KEY`, 기본 모델 ID `gemini-3.5-flash-lite`를 사용하며 `--model`로 변경할 수 있다. 텍스트와 `--image-path`로 지정한 로컬 JPG/PNG를 분석한다. 현재 FastAPI 라우터의 제공사를 Gemini로 전환한 것은 아니다.
- Gemini 구조화 출력은 `response_json_schema=ModelOutput.model_json_schema()`로 요청하고 `ModelOutput.model_validate_json()`으로 응답을 검증한다. 문자 관찰값은 값이 `null`이거나 `observedText`가 없거나 원문에 정확히 포함되지 않으면 제외한다. `values` 누락도 현재 모델에서는 빈 목록으로 처리된다. `--debug-extraction`을 명시한 로컬 실행에서만 원시 응답·파싱 결과·제외 사유를 출력한다. 이 출력에는 분석 원문이 포함될 수 있으며 운영 로그로 사용하지 않는다.
- **기존 직접 API 비교 기준:** `app/llm/`, [llm_api_validation.md](llm_api_validation.md), [provider_comparison.csv](provider_comparison.csv), [llm_handoff_IF02.md](llm_handoff_IF02.md)를 보존한다. 기존 API 접근 검증 보고와 이번 LangGraph 검증 범위를 구분한다.
- **사진은 YOLO+OCR+룰 전용:** 사진이나 임시 URL을 Bedrock에 전달하지 않는다. 현재 사진 경로의 자리에는 모델·OCR이 연결되지 않았으므로 처리 완료 ID를 만들지 않는다. 판독 결과는 `RawObservation`으로 연결하며 Spring의 저장·조회까지 검증한 뒤 사진 실연 여부를 결정한다.
- RAG는 Node가 별도 검색 함수를 호출하는 경계만 만든다. 제공된 저장소에 매뉴얼 인덱스/검색 구현이 없어 기본 구현은 빈 결과를 반환한다. 자료가 없을 때 해석을 생성하지 않으며 임의 예제 설명서를 코드에 넣지 않는다.
- 모델 학습·양자화·작업시간 추정·일정 계산·예약 API 호출·알림 생성·사장님 이관 상태 변경 등은 여기서 구현하지 않는다.

### 출력 검증 실패·외부 호출 실패

- FastAPI의 비동기 그래프는 boto3 Converse를 스레드에서 호출한다. 규칙 로드·문자 추출·사진 노드·최종 검증에 **하나의 전체 시간 예산**을 적용한다. `INTAKE_TIMEOUT_SECONDS` 기본값은 25초이며 `0 < 값 < 30`으로 제한한다.
- 제한 초과 시 로컬 대기를 취소하고 504 `AI_MODEL_TIMEOUT`을 반환한다. 실행 스레드는 호출이 종료될 때 SDK 클라이언트를 닫는다. 제공사 추론·과금 중단을 보장하지 않으며 부분 성공 결과는 반환하지 않는다.
- Bedrock 서비스 어댑터는 호출 실패·구조화 출력 오류 시 내부 재시도를 하지 않는다. API 명세 6.4에 따라 같은 입력의 재시도는 Spring에서만 관리한다.
- 규칙 파일은 그래프 실행 전에 로드하며, 없거나 잘못된 규칙을 임의 기본값으로 대체하지 않는다. FastAPI는 다음 오류를 공통 응답으로 반환한다.

| 상황 | HTTP / 오류 코드 |
| --- | --- |
| 내부 키 미설정·불일치 | 401 `UNAUTHORIZED` |
| 잘못된 요청 또는 `handlerMode=OWNER` | 400 `INVALID_INPUT` |
| JSON/Pydantic·결과 근거 검증 실패 | 502 `AI_OUTPUT_INVALID` |
| 키·모델 미설정, 기타 외부 모델 호출 실패 | 502 `AI_MODEL_ERROR` |
| 코드에서 식별하는 외부 호출 타임아웃 | 504 `AI_MODEL_TIMEOUT` |
| 전체 분석 시간 예산 초과 | 504 `AI_MODEL_TIMEOUT` |
| 업무 규칙 파일 로드 실패 | 503 `AI_RULES_UNAVAILABLE` |

구현 참고: [AWS Bedrock Converse](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html), [Python asyncio.timeout](https://docs.python.org/3.12/library/asyncio-task.html#asyncio.timeout).

### 사진 전달·처리 이력: Spring 연동 합의안

1. `photos`에는 원칙적으로 신규 또는 재시도가 필요한 미처리 사진만 HTTPS 임시 URL로 전달한다. 전체 사진 목록을 보내는 기존 방식도 `processedPhotoIds`와 함께 사용할 수 있다.
2. AI는 `processedPhotoIds`에 있는 사진 ID를 분석에서 제외한다. `fields`에 사진 근거가 있다는 이유만으로 해당 사진 전체의 처리가 완료됐다고 추정하지 않는다.
3. 성공 응답의 `analyzedPhotoIds`는 실제 호출이 완료된 사진 ID다. 추출된 글자·항목이 없거나 판독 불가여도 정상 분석이 완료되면 ID를 포함한다. 재분석을 건너뛴 사진은 포함하지 않는다.
4. Spring은 최신 입력 버전·응대 주체·중복 여부 검증을 통과해 결과를 저장할 때만 이 ID들을 처리 이력에 반영한다. 시간 초과·실패·STALE 응답으로 처리 완료를 기록하지 않는다.
5. 다음 요청에는 저장된 `fields`, `safetyFlag`, `processedPhotoIds`를 함께 전달한다. 새로 첨부한 사진 또는 새 ID로 재촬영한 사진은 다시 분석한다. AI 프로세스에 문의별 사진 캐시를 두지 않는다.

전체 사진 묶음 중 일부가 성공했더라도 이후 사진의 API 오류·출력 검증 실패·시간 초과가 발생하면 요청 전체가 오류이며 `result=null`이다. 앞서 성공한 사진의 ID도 정상 응답처럼 전달하지 않는다. Spring은 이 요청으로 처리 이력을 갱신하지 않고 미처리 사진을 다시 전달한다. 따라서 실패한 묶음의 재시도에서는 앞서 분석했던 사진을 다시 분석할 수 있다.

사진 처리 이력의 소유자는 Spring이며 문의·업체 범위에서 관리한다. **항목값·안전 상태·사진 처리 이력·요청 적용 기록을 같은 DB 트랜잭션으로 저장**하고, 저장 직전에 최신 `inputVersion`·응대 주체·중복 여부를 확인해야 한다. `STALE`, 인계 이후 응답, 중복 응답은 사진 이력만 따로 갱신해서도 안 된다. 이 원칙은 `SimulatedSpring`에서도 모의 검증하며 실제 Spring 트랜잭션 구현을 대신하지 않는다.

`processedPhotoIds`·`analyzedPhotoIds`와 기존 추가 필드 `intentEvidence`는 원래 명세 6.2~6.3 밖의 확장 계약이다. 이번 안정성 보완에서 필드를 더 추가하지 않았다. `processedPhotoIds` 생략 시 기본값 `[]`로 기존 요청은 유효하지만, 기존 방식으로 전체 사진을 매번 전달하면 처리 이력을 알 수 없어 다시 분석한다. Spring 요청/응답 DTO의 필드 수용 여부, 저장 위치·조회 범위·실패 시 재시도 정책은 배포 전에 합의해야 한다.

이 필드와 동작은 AI 측에서 구현·모의 검증한 연동안이다. Spring의 DTO·DB 처리 및 실제 신규 사진 전달 방식은 담당자와 합의·왕복 검증해야 한다.

## Jev 접근 조건·비교 과제 정리

- 이번 DS-05의 산출물은 접근 조건·비교 계획 정리다. 실제 선택형 판단 성능 비교는 AI-07에서 진행하며 현재 코드에 Jev 호출 경로는 연결하지 않았다.
- 비교 입력은 LLM이 구조화한 한국어 문의 상태(`facts`, `intent`, 업무 규칙)를 텍스트/JSON으로 전달하는 방안을 제안한다. 실제 제공 API가 받는 입력 형식·지원 출력 유형·인증 방식·계정 권한·요금·모델 버전은 사용 전 최신 문서와 실제 계정으로 확인해야 한다. 이번 로컬 검증으로 지원 여부를 확정하지 않는다. 이미지 직접 입력도 가정하지 않는다.
- 동일한 사례로 다음 행동 분류 정확도, 필수정보·안전 누락, 불필요한 반복 질문·사장님 이관, 응답 지연·비용을 LLM 기본 경로와 비교한다. 실제 고객 데이터 접근 범위와 한국어 품질도 별도 확인한다.
- 모델 학습·양자화 환경 구축은 선택 연구로 분리하며 기본 접수 흐름과 비교 계획 작성의 선행 조건으로 두지 않는다.

## 테스트·연동 확인

- 비용 없는 회귀 테스트: 정상 문자→`CHECK_SCHEDULE` 제안, 흐린 사진→미확인, 사진/진술 충돌→재확인, 필수/참고정보의 질문 한도 차이, 안전·명시적 연결 요청 우선 이관, OWNER 값 보호, 근거 ID 범위 위반, 내부 키·입출력 검증.
- 멀티턴 테스트: 같은 문의의 이전 `fields/askCounts/messages` 전달, 입력 버전 증가, 오래된 응답·중복 응답 적용 거부, 같은 근거 재추출·증상 표기 차이로 인한 재질문 방지, 실제로 다른 증상 제시 시 재확인을 확인한다.
- PR 후속 회귀 검증에는 [test_intake_pr_feedback.py](../tests/test_intake_pr_feedback.py), [test_intake_deadline.py](../tests/test_intake_deadline.py)를 추가했다. 모호한 증상 저장 후 보완, 기존 모호한 확정값 호환, 원문 근거가 있는 연결 요청, 조작·과거 근거 거부, 빈 사진 분석의 처리 이력, 신규 사진 선별, 문자·사진·재시도의 전체 시간 예산 및 SDK 취소·정리를 검증한다. 실제 모델 호출은 하지 않는 테스트다.
- 2026-11-05 최종 확인: 전체 테스트 **137개 통과**, 전체 Ruff 린트·포맷 검사 통과. 기존 WBS Mock 픽스처 `DEV-014`의 빈 작업 유형 문자열은 계약에 맞는 `null`로 교정했고 기대 행동은 유지했다. 이번 PR 후속 수정 검증에서는 실제 외부 API를 호출하지 않았다.
- 2026-11-06 안정성 보완 확인: 지정된 3개 회귀 테스트 파일 **129개 통과**, 전체 테스트 **234개 통과**. 이전 137개 테스트를 유지하고 97개 사례를 추가했다. 대상 이름만 있는 증상과 따뜻한 물 동의 표현, 근거가 일치하는 거절·과거 설명의 이관 방지, 정상 연결 요청 및 모델 판단 유지, 사진 부분 실패·재시도·시간 초과의 부분 결과 차단, STALE·인계·중복 응답의 사진 이력 불변을 검증했다. 실제 외부 API 호출과 실제 Spring DB 검증은 수행하지 않았다.
- 로컬 Gemini 실호출에서는 `보일러가 켜지는데 온수가 안나와요.` → `수리를 받고 싶어요.`의 2턴으로 추가 증상 질문 없이 `CHECK_SCHEDULE`에 도달한 사례를 확인했다. 이는 해당 사례의 확인이며 전체 모델 품질 평가나 Spring 연동 검증은 아니다.
- **Spring과 실제 통합 QA는 별도**다. 중복 요청·최신 버전·인계 상태·고객/업체 권한·실제 DB 저장 및 재시작 복원·주소/지역/연락처 인증·방문 시간 조회/선택/예약 확정을 11/3~11/9 연결 및 QA-02에서 확인할 계획이다. 이 문서는 실제 왕복 검증 완료를 선언하지 않는다.

### 재현 명령

```powershell
uv sync --locked
uv run pytest
uv run ruff format --check
uv run ruff check

# PR 피드백 1~3번 안정성 회귀 검증
uv run python -m pytest tests/test_intake_pr_feedback.py tests/test_intake_deadline.py tests/test_ilro_multiturn.py -v

# 외부 API를 호출하지 않는 고정 3턴 시뮬레이션
uv run python simulate_ilro_multiturn.py --mode mock

# 실제 Gemini 호출·과금이 발생하는 자유 대화
uv run python simulate_ilro_multiturn.py --mode gemini --interactive

# 단일 발화의 원시 응답과 관찰값 제외 사유 확인
uv run python try_my_intake_gemini.py --mode gemini --message "온수가 안 나와요" --debug-extraction
```

[simulate_ilro_multiturn.py](../simulate_ilro_multiturn.py)는 Python 메모리로 Spring을 모사하는 검증 도구다. 실제 DB나 예약 API에 접속하지 않으며 운영 대화 저장소로 사용하지 않는다. `CHECK_SCHEDULE/ESCALATE_TO_OWNER/COMPLETE/UNSERVICEABLE`을 받으면 수집 루프를 종료한다. 일정 안내 후 시간 입력을 받아 예약하는 기능은 이 시뮬레이터의 범위가 아니다.

기존의 추가질문 대상 저장 보류 가정(`defer_asked_field`)과 실험 옵션 `--persist-asked-field`는 제거했다. 이제 시뮬레이터도 AI가 반환한 `UNCONFIRMED` 증상을 그대로 저장하고 다음 턴의 구체적인 답변으로 갱신한다. Spring이 별도로 증상 구체성을 판정하는 규칙을 만들 필요가 없도록 상태를 반환하는 것이 이번 변경의 목적이다.

Gemini 모드의 최종 `PASS`는 기본적으로 문의·버전 일치, 결과 적용, 오래된 응답 폐기를 확인한다. 대화 품질과 중복 질문 여부까지 자동 보장하지 않는다. Mock 고정 시나리오에는 기대 행동·질문 대상 검사도 포함되며 별도 회귀 테스트가 재질문 방지 사례를 검증한다. 이 두 스크립트는 테스트에서 import하므로 함께 유지해야 한다.

## 미해결 항목·완료 판정의 제한

1. **증상 의미 판별:** 대상과 상태를 함께 확인하는 유한한 규칙이며 모든 동의 표현을 이해하는 것은 아니다. 관찰한 상태를 확인할 수 없는 표현은 추가 질문한다. 구체적인 증상끼리의 자유로운 바꿔 말하기는 여전히 문자열 차이로 충돌할 수 있으므로 평가 사례를 늘려야 한다.
2. **의도·사진 처리 계약:** 원문 근거와 요청·거절·과거 설명의 규칙을 함께 검사하지만 복잡한 중의적 표현의 의미 분류를 완전히 보장하지 않는다. 구분이 불확실하면 이관 의도를 확정하지 않는다. `intentEvidence`·`processedPhotoIds`·`analyzedPhotoIds`의 Spring 연동 및 전체 25초 제한과 실제 Spring의 30초 시도 설정의 정합성은 실환경 검증이 필요하다.
3. **작업 유형 현재값 계약:** 요청에는 고객이 선택·수정한 작업 유형의 현재값이 별도 포함되지 않는다. 현재 그래프는 고객 발화 전체에서 매번 다시 추출한다. 고객 수정값의 우선순위·유지 방식은 Spring과 합의해야 한다.
4. **업체별 정책·상담 문맥:** 현재 필수정보 설정은 전역 규칙 파일의 업무 유형별 설정이며 업체별 개별 정책은 없다. AI-02부터 최신 필드·출처·보완 횟수 및 AI/OWNER 질문을 포함한 대화를 함께 전달하며 고객 원문만 사실 근거로 인정한다. 모델명·사진·연식·설치 환경·비용 부담 주체를 일괄 필수화하지 않는다.
5. **미검증 범위:** Dev Bedrock 실호출, YOLO·OCR 실사진 연동, 매뉴얼 인덱스, Jev 실험, 실제 Spring·프론트 왕복은 완료 범위와 분리한다.
