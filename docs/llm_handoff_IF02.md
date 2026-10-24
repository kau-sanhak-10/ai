# IF-02 담당자에게 전달할 AI API 접근 검증 인계

## 담당 범위 분리
- **본 작업(멀티모달 API 접근·사용 준비):** 모델 접근 확인, 문자·사진 샘플, JSON 스키마, 오류 유형, 사용량/한도 확인, 기본·대체 후보 선정.
- **IF-02 및 후속 연동:** Spring Boot ↔ AI 실행 서버 인터페이스 정의, 인증·네트워크·배포·보안, 실제 기능 연계. API 접근 검증을 중복 수행하지 말고 본 결과 참조.
- **DS-05/AI-07:** Jev 접근/비교 및 선택형 판단 성능 실험은 별도 진행.

## 완료 기준 현황 (2026-10-23)

원래 3개 기준 중 후보 검증은 **부분 완료**, 키·실패·한도·사용량 확인은 **부분 완료**,
Jev 업무 분리는 **완료**다. Gemini 실제 호출·CSV 저장과 RPM/TPM/RPD는 확인 완료이며,
Gemini 사진 샘플의 모델명 `NGB 554`는 사용자 원본 대조로 일치를 확인했다.
GPT 정상 호출·한도·실제 사용량 검증은 **후속 작업으로 보류**하고 현재 기본 구현은 Gemini로 진행한다.
Gemini 결제·비용 한도와 프로젝트 일치, 운영 Secret 주입은 별도 확인이 남아 있다.
판정·체크리스트와 근거의 기준 문서는 `docs/llm_api_validation.md` 2·3절이다.
IF-02에서는 접근 검증을 반복 관리하지 않고 서비스 연동·배포 검증을 수행한다.

## 확정/조건부
- 기본 제공사: Google Gemini `gemini-3.5-flash-lite`. 2026-10-23 문자 요청 `426280c4b2e5`(495토큰), 사진 요청 `98a3b7fb0f8c`(1826토큰)의 JSON 출력과 CSV 저장 일치를 사용자 제공 자료로 확인했다.
- 같은 날 제공된 AI Studio 화면의 한도: RPM 15, 입력 TPM 250,000, RPD 500. 결제·비용 한도와 사용 키/콘솔 프로젝트 일치는 미확인이다. 접근 검증 근거는 `docs/llm_api_validation.md` 3절에서만 관리하며 IF-02는 참조한다.
- 대체 후보: OpenAI `gpt-4.1-mini`, 코드 작성/모의 테스트 완료. 크레딧 부족 응답은 보고됐으나 정상 실호출은 미검증이다. 정상 호출·한도·실제 사용량 검증은 후속 보류하며 현재 즉시 자동 전환 대상으로 사용하지 않는다.
- 서버 호출 경로는 Gemini `analyze_gemini()`, OpenAI `call_openai_from_environment()`이며 `Settings(_env_file=None)`으로 서버 환경변수만 읽는다. LLM 호출 경로의 로컬 `.env` 로드는 `app.llm.cli`에서 수행한다. 실행·한도 확인 절차는 `docs/llm_api_validation.md` 7절 참조.
- Gemini는 CSV에 JSON 검증 실패 시에도 응답 사용량과 실패를 기록하며 `HttpRetryOptions(attempts=1)`로 한 번만 호출한다. HTTP 429 원인이 불명확하면 콘솔에서 한도를 확인한다.
- OpenAI 요청별 SQLite 로그와 429 오류 분류/자동 재시도 차단을 구현했다. 키·고객 원문·사진·상세 오류 문구는 로그에 저장하지 않는다.
- 운영 서버의 Secret 주입·권한, 프론트 비노출, 로그 DB 영속성·접근권한·보존기간 검증은 배포 환경이 없어 미완료이며 IF-02 배포 단계에서 확인한다.

## 입력/출력 인터페이스 (제안)
- 입력: `message: str`, `image?: JPG/PNG`.
- 추출 필드: `manufacturer`, `model_name`, `error_code`, `symptom`, `address` (누락값은 null).
- 근거: `evidence[]`(`field_name`, `source`, `observed_text`), `visible_image_text[]`, `uncertainties[]`.
- 응답 메타데이터: `provider`, `model`, `request_id`, `image_attached`, `missing_fields`, `needs_review`, `usage`.
- API 인증·요청 ID 관리·Spring Boot 서비스 데이터 검증은 연동 담당이 정의해야 함.
- Gemini 응답 형식의 `make_output()` 함수에 공급사 이름이 고정되어 있으므로, 다중 제공사 통합 때는 provider를 매개변수로 분리해야 함.

## 실패 대응 권고
- 크레딧 부족·인증 오류: 재시도 금지, 사용자에게 일시 중단 안내 및 관리자 알림.
- OpenAI 호출은 `max_retries=0`으로 자동 재시도를 끄며, 429 로그에서 크레딧 소진과 속도 제한을 구분한다. 실제 계정별 한도값은 제공사 콘솔에서 확인한다.
- 503/504: 제한적 재시도 여부 확인 후 작업 상태 보존, 재시도 중복/중복 과금 주의.
- 구조화 출력 오류 또는 확인되지 않은 모델명: 추측값을 사용하지 않고 확인 필요 처리.
- 로그: 키/사진/주소/원본 대화 저장 금지; 요청 ID, 유형, 사용량만 최소 기록.
