# AI-13 예약 단계 판단·프론트 예약 연결

기준일: 2026-11-25. 관련 기능: **C-09·C-12·C-13 (모두 P0)**.
첨부 WBS의 완료 기준을 기능 요구사항으로 참고했다. 공유 Dev 배포·실제 문자 발송·예약 저장은
이번 로컬 수정의 검증 범위에 포함하지 않는다.

## 역할과 실행 흐름

1. Spring이 최신 문의 정보를 `POST /internal/v1/inquiries/analyze`로 보낸다.
2. AI는 추출 → 규칙 판단 → 응답 생성 → 출력 검증을 수행한다.
   작업 유형·업무 필수정보·안전·사람 응대 요청·업체 설정이 허용되면
   `nextAction.action=CHECK_SCHEDULE`을 반환한다. 예약 시간·작업시간·예약 ID는 만들지 않는다.
3. Spring이 `requestId` 중복과 최신 `inputVersion`·응대 주체를 검사하고 결과를 저장한다.
   유효한 `CHECK_SCHEDULE`은 문의 상태 `SCHEDULING`으로 반영한다.
4. 프론트는 문의 상세의 **`status=SCHEDULING` + `handlerMode=AI` +
   `nextAction=CHECK_SCHEDULE`**을 확인하고 고객이 시간 선택 화면으로 진입하게 한다.
   내부 AI 응답의 `nextAction`은 객체지만, 현재 Spring 고객 상세 응답은 **문자열**이다.
5. 프론트가 가능시간을 조회하고 고객에게 선택·문자 인증·최종 확인을 받는다.
6. 프론트가 확정 API를 호출한다. Spring이 업체별 자동 예약 허용·필수정보·안전·지역·인증·
   작업시간·이동시간·최신 일정·동시 요청을 다시 검사하고 저장한다.

AI의 내부 `stage`는 LangGraph 실행 단계이며 고객의 예약 단계가 아니다. 외부 AI 계약에
`bookingStage` 같은 새 필드를 추가하지 않았다. 프론트의 `bookingStage`는 Spring 상세 응답에서
계산하는 화면 내부 값이다. 참고정보만 미확인이면 예약 흐름을 계속한다.

## 구현

- `app/intake/contracts.py`: `CHECK_SCHEDULE`과 안전 미해결·사람 응대 요청·미확정 작업 유형·
  추가 확인 대상이 함께 반환되는 모순을 Pydantic으로 거부한다.
- `app/intake/graph.py`: 반환 직전 `validate_schedule_handoff()`가 요청의 응대 주체·저장된 안전
  상태·업체 설정·제공 작업·작업별 필수정보·충돌을 검증한다. 기존 `effective_fields()`를 재사용한다.
- `tests/test_ai13_booking_handoff.py`: 잘못된 반환값과 변경된 요청에 대한 차단, FastAPI→실제
  LangGraph→모의 Bedrock SDK의 정상·예외 분기를 검사한다. 외부 모델을 실제 호출하지 않는다.
- 프론트 로컬 수정본: `artifacts/ai13/frontend`. 원격 저장소에는 반영하지 않았다.
  `COLLECTING`을 예약 후보로 취급하던 동작을 고치고, 조회·확정 직전 문의를 다시 읽는다.
  아직 수집 중이거나 Spring이 판단을 제공하지 않으면 상담으로 돌아가 다시 확인하도록 한다.
  모든 상태에서 판단의 입력 버전이 최신인지는 이 재조회만으로 보장하지 않는다.
  새 확정 응답을 받지 못한 경우에는 같은 요청 키와 본문으로 결과를 재확인한다.

프론트 변경 파일은 다음과 같다. 경로는 로컬 프론트 저장소 기준이다.

| 파일 | 변경 목적 |
| --- | --- |
| `src/lib/customer-api.ts` | 최신 `nextAction`과 문의 상태·응대 주체로 화면의 예약 단계 계산 |
| `src/lib/customer-booking-api.ts` | 후보·확정 전 8초 제한으로 문의 재조회, 신규 중복 확정 차단, 응답 유실 재확인 때만 기존 키의 확정 요청 허용 |
| `src/lib/customer-booking.ts` | 접수 내용 변경·수집 중인 경우의 화면 내부 오류와 안내 추가 |
| `src/app/customer/booking.tsx` | 수집 중 오류에서 확정 버튼 차단, 응답 유실 재확인 여부 전달 |
| `src/app/customer/[linkToken]/visit.tsx` | 공통 예약 API 검사를 재사용하고 수집 중 안내·단계 재조회 버튼 연결 |
| `tests/customer-ai-booking.test.mjs` | 단계별 진입·인계 뒤 호출 차단·동일 키 재확인·신규 중복 차단 테스트 추가 |
| `tests/customer-booking.test.mjs` | 기존 예약 API 테스트에 실제 순서인 문의 재조회 반영 |
| `tests/customer-live-booking.browser.mjs` | 실제 화면의 수집 중 차단·단계 재조회·선택 후 인계 차단 검사 추가 |
| `docs/customer-booking-integration.md` | 연결 조건과 로컬 검사·실연동 검증 범위 기록 |

AI 저장소의 `README.md`에는 이 문서의 링크를 추가했다. `artifacts/ai13/package_changes.py`는
이번 변경 파일만 패치·검증 JSON·ZIP으로 묶으며 사용자 기존 `.env.example` 변경은 포함하지 않는다.

## 프론트와 Spring 사이의 API

모든 고객 API는 `Authorization: Bearer <문의 토큰>`을 사용하고 공통 응답의 `code`로 분기한다.
토큰·문자 코드·실제 고객 정보는 소스나 검증 산출물에 저장하지 않는다.

| 목적 | 요청 | 판단 기준 |
| --- | --- | --- |
| 최신 예약 단계 | `GET /api/v1/customer/inquiries/{inquiryId}` | 위 세 조건을 모두 만족할 때 신규 시간 선택 허용 |
| 가능시간 조회 | `GET /api/v1/customer/inquiries/{inquiryId}/available-slots?from=YYYY-MM-DD&days=3` | Spring의 `workMinutes`, `days[].slots`만 표시 |
| 고객 최종 확정 | `POST /api/v1/customer/inquiries/{inquiryId}/reservations` | `Idempotency-Key: <UUID>`, 본문은 `{ "startAt": "시간대 포함 시각" }` |
| 기존 예약 확인 | `GET /api/v1/customer/reservations/{reservationId}` | 서버의 예약 상태를 표시, 후보 선택값으로 확정을 추정하지 않음 |

처음 저장은 201, 같은 키·요청의 재확인은 200이다. 같은 키로 다른 시각을 보내지 않는다.
프론트 재조회와 확정 사이의 상태 변경까지 막는 최종 책임은 Spring의 트랜잭션 검증에 있다.

| 상황 | 처리 |
| --- | --- |
| `COLLECTING`, 판단 없음, 알 수 없는 상태 | 신규 후보·확정 호출 차단, 상담에서 안내 확인 후 예약 단계 다시 조회 |
| `OWNER_CHECK`·사장님 응대 | 신규 후보·확정 호출 차단, 사장님 안내. 이미 연결된 예약 조회는 유지 |
| `PHONE_NOT_VERIFIED` | 선택 시간 유지, 문자 인증. 번호 변경·만료 시 재인증 |
| `SLOT_UNAVAILABLE` (`PAST` 포함) | 확정으로 표시하지 않고 최신 후보 재조회·재선택 |
| `RESERVATION_ALREADY_EXISTS` | 기존 예약 조회 |
| `IDEMPOTENCY_KEY_REUSED` | 새 후보·요청 내용 재확인. 다른 본문으로 이전 키 재사용 금지 |
| 422: 자동 예약 비활성·작업 미확인·필수정보 미해결·안전·지역·이동시간 | 서버 코드에 맞는 안내와 사장님 확인 |
| 확정 응답 유실·서버 오류 | 확정으로 추정하지 않고 동일 키·본문으로 결과 재확인 |
| 문의 재조회 오류 | 후보·확정 API를 호출하지 않고 재시도 안내 |
| 오래된 AI 결과·중복 AI 결과 | Spring이 입력 버전·요청 ID로 미적용 처리. AI 메모리로 중복 예약을 막지 않음 |

## 확인한 소스와 명세 차이

- 기능 명세서는 UTF-8-SIG로 읽어 C-09·C-12·C-13의 선행 조건·예외·P0 우선순위를 확인했다.
- API 명세 1장·2장·3.5·3.8·4.2·4.9·5.2·6장을 기준으로 요청·응답 필드와 역할을 유지했다.
- [프론트 기준 커밋](https://github.com/kau-sanhak-10/frontend/tree/25c78f4756af56224a27248f60cdd62c12b3f98f):
  고객 예약 API·화면·인증·재시도 기능이 이미 있다. 이를 재사용했다.
- [Spring 기준 커밋](https://github.com/kau-sanhak-10/backend/tree/50cde823a7097bf3e4f9963cc4a0b7ae14d4d547):
  `AiAnalysisStore.moveStatus()`의 `CHECK_SCHEDULE → SCHEDULING`,
  `CustomerInquiryService.detail()`의 `hasCurrentNextAction()` 필터,
  `InquiryDetailResponse.nextAction`의 문자열 계약을 코드로 확인했다.
  소스 확인은 해당 커밋의 실제 배포 완료를 뜻하지 않는다.
- 11/26 추가 확인: Spring의 `AiDecision.hasCurrentNextAction()`은 `OWNER_CHECK` 상태에서만
  `inputVersion`과 현재 입력 순번을 비교한다. 프론트가 문의를 다시 조회하더라도
  `SCHEDULING`을 포함한 모든 상태에서 오래된 `CHECK_SCHEDULE`이 배제된다고 보장할 수 없다.
  Spring의 최신 판단 제공 조건은 별도 확인·보완이 필요하다.
- 명세 6.3의 ‘충돌이 있으면 자동 예약 금지’ 문장과 10/26의 참고정보 예외가 함께 남아 있다.
  기존 코드·WBS·기능 명세의 작업별 필수정보 정책을 유지했다. 참고정보 충돌은 남은 보완 질문
  횟수 안에서 확인하고, 한도 후 그 사유만으로 사람에게 넘기지 않는다.
- 업체의 **작업별** `auto_booking_allowed`는 현재 `Business` 입력에 없다.
  AI가 이를 확인했다고 주장하지 않으며 Spring의 최종 검사로 유지한다.
- 명세 3.8의 재호출 문구와 6.4의 재시도 단일 책임이 다르다. 기존 구현과 6.4에 따라 AI·SDK는
  자동 재시도하지 않고 Spring이 재시도 예산을 관리한다.

## 실행과 통합 확인

AI 저장소에서:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_ai13_booking_handoff.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check app/intake/contracts.py app/intake/graph.py tests/test_ai13_booking_handoff.py
```

프론트 로컬 수정본에서 Node.js 24.x·npm 11.x와 기존 잠금 파일을 사용한다:

```powershell
Set-Location artifacts/ai13/frontend
npm.cmd ci
npm.cmd run check
npm.cmd run build
```

실제 연동은 테스트 업체에서 별도로 확인한다. `CUSTOMER_BOOKING_ENABLED`는 이번 수정에서
켜지 않았다. 배포 설정·실제 문자·공유 DB 예약 생성은 담당자와 확인한 테스트 환경에서 수행한다.

- 같은 문의로 AI 분석 → Spring의 `SCHEDULING`·최신 `CHECK_SCHEDULE` → 화면 버튼 → 후보 조회를 확인한다.
- 선택 후 안전 신고·직접 상담·필수정보 수정 시 기존 후보로 확정 API가 호출되지 않는지 확인한다.
- 참고 모델 미확인만으로 `OWNER_CHECK`·사장님 알림이 생기지 않는지 확인한다.
- 문자 인증 중 시간 마감, 동일 키 재전송, 다른 키 동시 확정, 응답 유실 후 재확인을 검사한다.
- Spring 잠금·중복 제약·최신 버전 검사와 실제 DB의 예약·알림 건수를 확인한다.

자동 검사 통과와 실제 웹→Spring→AI 왕복·예약 저장의 통합 검증 완료는 구분한다.

## 2026-11-25 실행 결과

| 실제 실행한 검사 | 결과 |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest -q` | AI 전체 635개 통과 |
| `.venv\Scripts\ruff.exe check app/intake/contracts.py app/intake/graph.py tests/test_ai13_booking_handoff.py --no-cache` | 통과 |
| 같은 Python 파일 대상 `ruff format --check` | 통과 |
| 프론트 `npm.cmd run check` | Prettier·ESLint·TypeScript·단위 테스트 145개 통과 |
| 프론트 `npm.cmd run build` | Next.js 프로덕션 빌드 통과 |
| `node tests/customer-live-booking.browser.mjs` | Edge headless + 로컬 모의 API: 수집 중 차단·단계 재조회·선택 후 인계 시 확정 차단·문자 오류·응답 유실 재확인·기존 예약 조회·상담 복구 통과 |
| `node tests/smoke.mjs` | 진입·홈 복귀·404·모바일 viewport·manifest·아이콘 통과 |

PC의 PATH에 Node.js가 없어 `artifacts/ai13/tools/node-v24.13.0-win-x64`의 Node.js 24.13.0·
npm 11.6.2를 실행 경로에 넣었다. 프론트의 `.nvmrc` 버전을 따랐으며 잠금 파일은 변경하지 않았다.
기존 브라우저 스크립트는 별도 도구 폴더의 Playwright와 설치된 Edge로 실행했다.
브라우저 검증 서버만 loopback에 열어 테스트용 접수·예약·복구 스위치를 켰고, 검사 후 종료했다.
실제 배포 설정·원격 커밋·push·병합은 수행하지 않았다.

변경 전달용 `artifacts/ai13/frontend-ai13.patch`는 프론트 저장소 루트에서
`git apply --check <패치 경로>`로 확인한 뒤 `git apply <패치 경로>`로 적용할 수 있다.
기준 커밋 이후 같은 파일이 바뀌었다면 내용을 비교한 뒤 적용한다. 패치는 원격 저장소에 자동 반영되지 않는다.
