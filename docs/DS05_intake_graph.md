# DS-05 · AI 입출력 / LangGraph 처리 흐름 (WBS 범위)

기준: `api-명세서_version2.txt` 6장 및 10/26 운영 기준, `develop` 커밋 `f06a389`.

## 담당 시스템과 범위

- 원민재: FastAPI `POST /internal/v1/inquiries/analyze`, LangGraph State·8개 Node·조건부 Edge, 공통 응답/출력 검증.
- 정채우: Spring 비동기 호출, DB에 있는 최신 문의 상태의 구성, `ai_input_seq`/`handler_mode`/`OWNER` 출처 검증, 상태·이력·알림·예약 저장.
- 박준형: 내부 접속·권한·배포 지원.
- 프론트: Spring의 방문 시간 조회, 고객 선택 및 최종 확인, 예약 확정 API 호출.

**AI에는 예약/이관 상태를 수정하는 도구와 DB 연결이 없다.** `CHECK_SCHEDULE`은 화면 진입 행동 **제안**일 뿐 최종 예약 가능 판정이 아니다. `handlerMode=OWNER` 상태로 새로 AI를 호출하지 말아야 하고, 중간에 OWNER로 바뀐 결과는 Spring이 버전과 응대 주체 확인 뒤 폐기한다.

## API

- 경로: `POST /internal/v1/inquiries/analyze`, 헤더: `X-Internal-Key` (Spring과 공유하는 비밀키).
- 요청: 명세 6.2 `requestId`, `inquiryId`, `ownerId`, `inputVersion`, `handlerMode`, `business`, `fields`, `askCounts`, `messages`, `photos`, `safetyFlag`.
- 응답: `ApiResponse[AnalyzeResult]` — `{success,code,message,result}`. 결과는 명세 6.3의 `facts`, `conflicts`, `interpretations`, `workCategory`, `safety`, `intent`, `nextAction`, `reply` 및 버전·모델 사용량 메타데이터.
- 이름·주소·전화번호·토큰을 AI 요청 또는 로그에 넣지 않는다. 접근 가능한 문의/업체·사진 ID의 **소유권 확인은 Spring의 책임**이다. AI는 인증된 내부 키와 그 요청에 있는 근거 ID의 일치 여부를 검사한다.
- 데이터 원본: Spring DB. AI 상태 저장소/Checkpointer 없음. 재시작 후 다음 요청은 저장된 `fields`, `askCounts`, `messages`, `inputVersion`으로 다시 분석한다. 개인별 처리 단계는 원본 상태와 `nextAction`으로 판단하며 AI 프로세스 메모리에 의존하지 않는다.

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
| extract_message | messages, 기존 fields (Spring 입력) | observations, intent, workCategory, safety, stage=EXTRACT | 추출 모델 형식 오류 재시도 1회, 이후 502 |
| analyze_image | photos | observations(누적) | 문자 판독 불가는 UNCONFIRMED, 호출 실패는 오류 |
| merge_facts | fields, observations | facts, conflicts | OWNER 값 덮어쓰기 금지, 충돌 시 확인 유도 |
| check_rules | facts, conflicts, safety, handlerMode | 강제 nextAction 또는 null, stage=DECIDE | 규칙 로드 실패 503 |
| decide_action | 원본 및 확인 정보, askCounts, workCategory, business | nextAction | 질문 한도·안전·예약 단계 규칙 검사 |
| search_manual | 이전 messages, 모델·제조사 | manualHits | 확인한 해당 제품의 자료만 허용; 자료 없으면 빈 검색 |
| generate_reply | nextAction, manualHits | reply, interpretations, stage=SUMMARY | 예약·이관 완료를 허위로 확정하지 않음 |
| validate_result | facts, conflicts, nextAction, reply, 사진/메시지 근거 ID | result | 출력 불일치 502, Spring이 최신 inputVersion 재검증 |

- Node State는 원본 입력 + 후속 단계에 필요한 **구조화된 Raw 값**만 보관한다. 사진 존재 여부·질문 횟수 합계·예약 슬롯 같은 파생값은 보관하지 않는다.
- `askCounts[field]`: Spring이 넘긴, **이미 발송한 질문 횟수**. 최초 질문 1회 + 보완 요청 최대 2회 = 합계 3회로 해석한다. 작업 유형 질문은 `targetFieldKeys=["work_category"]`로 표시하고 동일 키로 횟수를 누적해야 한다. 이 키를 기존 백엔드가 허용하는지는 연동 전 확인이 필요하다.
- 단순 참고정보(사진/모델/오류코드)의 미확인만으로 이관·알림·예약 차단하지 않는다. 반면 **정보 충돌**, 필수정보 미해결(보완 한도 초과), 안전 문제, 고객의 직접 연결 요청은 기존 강제 확인 규칙을 따른다.
- 업체·업무별 필수 항목은 `rules/inquiry_types.yaml`(YAML 1.2 호환 JSON 문법)에서 읽는다. 확인되지 않은 작업별 예외 항목을 추측해 추가하지 않았으며 초기에는 공통 증상만 선언했다. 추가 검증 없이 모델명·사진을 전 문의의 필수 항목으로 만들지 않는다.
- `decidedBy=RULE`: 현재 행동 선정 함수는 검증 규칙 기반이고, 외부 LLM은 대화/사진의 원시 관찰값과 의도/작업 유형을 구조화하는 역할이다.

## 모델·외부 모듈 경계

- 이전 OpenAI/Gemini 직접 API 실험 코드(`app/llm`)는 변경하지 않았다. 현재 서비스 기본 경로에는 **기존에 의존성이 있는 OpenAI Responses API**를 통한 문자/사진 입력 어댑터만 연결했다. 호출하려면 `OPENAI_API_KEY`와 `OPENAI_MODEL` 환경변수의 실제 검증된 값을 설정한다. 키·모델이 없으면 실제 응답을 만들어내지 않고 `AI_MODEL_ERROR`다.
- Bedrock 멀티모달 모델/리전/권한/비용은 실호출 전까지 확정하지 않는다. YOLO+OCR+룰은 별도 실행 환경이다. 두 방식을 비교하기 위한 공통 후보 구조는 `RawObservation(fieldKey,value,source,evidence,unconfirmedReason,observedText)`이고 Node는 `ExtractionProvider` 인터페이스에 의존한다. **어느 경로도 매 사진에 무조건 병렬 실행하지 않는다.** 실제 Bedrock·YOLO 연동/비교 실험 자체는 WBS 다음 연구·검증 작업이며 이번 코드에 가짜 SDK 호출을 만들지 않는다.
- RAG는 Node가 별도 검색 함수를 호출하는 경계만 만든다. 제공된 저장소에 매뉴얼 인덱스/검색 구현이 없어 기본 구현은 빈 결과를 반환한다. 자료가 없을 때 해석을 생성하지 않으며 임의 예제 설명서를 코드에 넣지 않는다.
- 모델 학습·양자화·작업시간 추정·일정 계산·예약 API 호출·알림 생성·사장님 이관 상태 변경 등은 여기서 구현하지 않는다.

## Jev 비교 과제 (선택 연구, 구현 범위 밖)

- 비교 후보 입력: LLM이 먼저 구조화한 한국어 문의 상태(`facts`, `intent`, 업무 규칙) 기반 분류. 제공업체 문서에서 텍스트/JSON/텍스트 배열 상태와 choice/score/noul 타입을 확인할 수 있다.
- API 키/권한이 필요한 외부 모델이며, 실제 사용 전 접근 권한·요금·모델 버전·한국어 품질·보안 조건을 검증해야 한다. 직접 이미지 입력 지원 여부는 제공 서비스와 모델 버전마다 다를 수 있으므로 **이번 결정 경로에서는 가정하지 않는다**.
- 동일한 평가 사례로 다음 행동 분류 정확도, 위험 항목 누락, 응답 지연, 비용을 LLM 기본 경로와 비교한다. 모델 학습/양자화·환경 구축은 이번 과제의 선행 조건이 아니다.

## 테스트·연동 확인

- 무료 Mock 사례: 정상 문자 입력→CHECK_SCHEDULE, 흐린 사진→미확인, 사진/진술 충돌→재확인, 질문 한도, 위험 신호→이관, 사장님 수정값 보호, 근거 ID 범위 위반, 내부 키 검증.
- API 6.2 요청에는 **고객 화면에서 선택·수정한 작업 유형의 현재값**이 별도 포함되어 있지 않다. 따라서 실제 선택값을 AI가 재확인 없이 이어받는 계약은 백엔드와 아직 합의하지 못했다. 이 파일에서는 API 외 필드를 임의 추가하지 않았다.
- **Spring과 실제 통합 QA는 별도**: 중복 requestId, `inputVersion` 변경 뒤 도착한 결과(STALE), OWNER 모드 전환, 예약·알림 DB 처리, 주소·지역·휴대전화 인증 검증. AI 서버가 단독으로 완료했다고 표시하지 않는다.
- 프로젝트와 패키지 설치: `uv sync --locked`; 테스트: `uv run pytest`, `uv run ruff format --check`, `uv run ruff check`.
