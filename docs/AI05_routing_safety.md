# AI-05 처리 분기·안전 규칙 연결

작업일: 2026-11-19. 관련 기능: C-09(P0), C-10(P1), B-15(P0), B-16(P1).
범위 정리: 2026-11-20. 기존 API 명세를 기준으로 미합의 확장을 제외하고 재시도 영속화를 검증 항목으로 바꿨다.
PR 피드백 반영: 2026-11-25. 모델의 작업 유형만으로 서비스 불가를 확정하지 않고, 부품/본체 교체를 구분한다.
기준: 첨부 AI-05 WBS, UTF-8-SIG로 읽은 `기능 명세서.csv`, `api-명세서.md` 6.1~6.4,
`service_contract.md`, `AI_QA02_question_policy.md`.

## 구현한 판단과 우선순위

1. 안전 문제·고객의 직접 상담 요청은 `ESCALATE_TO_OWNER`로 우선 처리한다.
2. 추출된 작업 유형이 `business.activeWorkCategories`에 없으면 `ESCALATE_TO_OWNER`를 반환한다.
   모델 오분류만으로 상담을 종료하지 않고 Spring의 사장님 확인·알림 경로로 연결한다.
3. 제공 작업이면 기존의 필수/참고정보 분리·충돌 확인·항목별 보완 한도를 적용한다.
4. 작업 유형 미확인은 대화로 확인하고 한도 후 이관한다. 고객에게 유형 선택을 강제하지 않는다.
5. 제공 작업의 자동 예약만 꺼져 있으면 사장님에게 전달한다.
6. 조건을 만족하면 `CHECK_SCHEDULE`을 반환한다. 지역·인증·일정·최종 예약은 Spring이 검사한다.

규칙 버전은 `rules-ai05-v3`, 현재 프롬프트 버전은 `intake-ai05-v5`다.
초기 `intake-ai05-v1`의 실제 Bedrock 검증에서 확인한 추출 문제를 보완했으며,
실행 이력과 제한은 [Bedrock 실검증 기록](AI05_bedrock_live_20261119.md)에 정리했다.
`UNSERVICEABLE`은 기존 응답 enum으로 유지하지만 모델의 작업 유형과 제공 목록 불일치만으로
자동 선택하지 않는다. 최종 서비스 불가는 백엔드·사장님의 확인으로 처리한다.
지역을 판단하거나 `OUT_OF_AREA`를 임의로 반환하지 않고 예약·알림 API도 직접 호출하지 않는다.

### 교체 대상과 작업 유형 검증

백엔드 `WorkCategory`와 PR 피드백의 의미를 따른다. `REPAIR`는 수리·부품 교체,
`LEAK`는 누수 수리, `REPLACEMENT`는 보일러 본체 교체다.
교체라는 단어만으로 `REPLACEMENT/CONFIRMED`를 반환하도록 한 지시를 제거했다.

프롬프트는 작업 종류와 대상을 함께 추출한다. `work_category_rules.py`는 모델이 확정한
REPLACEMENT 후보에 고객 원문의 교체 대상 근거가 있는지 추가 검사한다.
명시적인 밸브·펌프·배관 등 부품 교체는 REPAIR로 보정하고, 본체 교체 근거가 없거나
본체·부품 요청이 함께 섞였으면 미확인으로 두어 기존 작업 유형 질문·한도 정책을 적용한다.
AI/OWNER 메시지는 대상 근거로 사용하지 않으며 모델의 미확인 후보를 임의로 확정하지 않는다.
유한한 대상·표현 규칙이므로 모든 한국어 표현의 의미 판별을 보장하는 검증기는 아니다.

## 기존 API 계약과 미합의 확장 보류

WBS의 요구사항은 고객이 작업 유형을 수정할 수 있고 최신 상태에 맞게 처리되어야 한다는 것이다.
`currentWorkCategory`라는 요청 필드 자체를 요구하지는 않는다. 원래 API 6.2에도 이 필드는 없다.
2026-11-20 사용자 지시에 따라 이 필드를 요청 모델·그래프·모델 입력·모의 Spring에서 제거했다.
API 명세에 추가했던 선택 확장 단락도 제거했으며, 기존 요청·응답 계약과 알 수 없는 필드 거부를 유지한다.

- AI는 실제 고객 대화에서 작업 유형을 추출한다. 작업 유형 미확인은 기존 보완 질문·이관 기준을 따른다.
- Spring이 DB의 고객 수정값을 보존하고 최종 예약 조건을 검사한다. 11/20 조회한
  `AiAnalysisStore.applyWorkCategory()`는 DB에 작업 유형이 있으면 AI 결과로 덮어쓰지 않는다.
- 기존 요청에는 화면에서 수정한 현재 유형이 별도로 없으므로 AI의 안내까지 그 값에 맞는다고
  보장하지 않는다. 고객 유형 수정 후 재질문·잘못된 서비스 불가 안내를 Dev에서 검증한다.
- 불일치가 확인되면 기존 계약으로 처리 가능한지 검토하고, 필요할 때만 팀과 최소 API 변경을 합의한다.
  실제 고객 메시지를 위조하거나 다른 필드에 작업 유형을 임의로 넣어 우회하지 않는다.

기존 백엔드 패치는 [보류 제안 보존본](patches/ai05-current-work-category.withdrawn.txt)으로 옮겼다.
이는 적용·배포 대상이나 WBS 완료의 선행 조건이 아니다. 백엔드 소스·DB·재시도 구현은 수정하지 않았다.

## 사장님 확인·재개와 복구

`OWNER + CONFIRMED` 증상은 Spring이 확인한 값으로 사용한다. 일반 고객 발화의 구체성을
검사하는 문자열 규칙 때문에 다시 질문하지 않는다. `UNCONFIRMED`, 빈 값, 필수정보 충돌은
계속 예약을 차단한다. OWNER 값에 대해 다른 근거가 있는 충돌도 기존 확인 정책을 유지한다.

AI는 요청 밖에 문의 상태를 저장하지 않는다. 다음 요청의 `fields`, `askCounts`, `messages`,
`safetyFlag`, 사진 처리 이력으로 다시 실행한다. 별도 체크포인터나
AI 문의 DB를 추가하지 않았다. 다음 검사는 실제 별도 Python 프로세스에서 FastAPI를 실행하고
종료한 뒤 새 프로세스를 시작하지만, Bedrock SDK와 Spring 저장은 대역을 사용한다.

- 재기동 후 OWNER 확인값을 재사용하고 고객 대화에서 작업 유형을 추출하며 확인한 증상을 재질문하지 않음.
- 이전 프로세스의 성공 응답을 다시 받아도 모의 Spring에서 답변을 중복 저장하지 않음.
- 호출 실패 후 새 프로세스의 결과를 적용하며 실패 때문에 질문 횟수·필드가 바뀌지 않음.
- 최신 입력·사장님 전환·전환 후 재개에서 이전 버전 응답을 적용하지 않음.
- 저장된 안전 플래그는 재기동 후에도 모델 호출 없이 이관하며 `OWNER` 분석은 거부함.

**실제 Spring DB 재시작·사장님 화면 전환·예약 중복 방지는 위 검사에 포함되지 않는다.**
프로세스를 다시 시작해도 AI가 상태를 복원할 수 있다는 검증과 백엔드의 복구 작업 재개 검증을 구분한다.

## 실패·재시도 계약

FastAPI/SDK는 분석 1회만 실행한다. 전체 분석 시간 예산은 기본 25초이며 30초 미만이다.
SDK의 `total_max_attempts=1`을 유지한다. 타임아웃은 504 `AI_MODEL_TIMEOUT`, 출력 오류는
502 `AI_OUTPUT_INVALID`, 권한·할당량 등 모델 오류는 502 `AI_MODEL_ERROR`, `result=null`이다.
제공사 예외 원문·자격 증명을 응답이나 운영 로그로 내보내지 않는다.

2026-11-19 GitHub backend `develop` 코드에서 확인한 내용:

- `HttpAiAnalyzeClient.java` blob `536ac8a8d70a217dbdbf43409903f9053db41136`:
  `AI_MODEL_ERROR`는 재시도하지 않는다. 권한·크레딧·할당량과 일시 오류가 같은 코드이므로
  모델 오류 전반을 재시도하지 않는 보수적인 정책이다. 새 오류 코드·헤더를 추가하지 않는다.
- `AiAnalysisService.java` blob `412208176e4b571077997ff9531b941355d39f7e`:
  `MAX_ATTEMPTS=2`, 저장된 실패 횟수부터 이어서 시도하고, 매번 남은 예산으로 제한한다.
  실패 기록 뒤 최신 입력·응대 주체가 달라졌으면 이전 작업을 종료한다.
- `AiAnalysisQueue.java` blob `5a080cb0f42296c452a949b5ee216cb96b7de46b`:
  주기적으로 DB에서 미응답 문의를 찾아 다시 요청한다. 대상은 90초 이상 지연되고 24시간
  이내인 문의이며 한 번에 최대 50개다. 이 복구 코드의 존재와 실제 중단·재기동 성공은 구분한다.
- 시도 시간과 전체 예산은 Spring 환경설정 값이다. 소스 조회만으로 실제 Dev 값이
  30초·60초인지, 재시작 전후 전체 예산이 보존되는지 확인할 수 없다.

### 재시도 영속화는 확정 개발이 아닌 검증 항목

API 6.4는 같은 입력 최대 2회·시도당 최대 30초·전체 대기 예산 60초와 Spring의 재시도 관리를 요구한다.
최초 deadline을 DB에 저장하는 구체적 구현 방식은 명시하지 않는다.
`callUntilSettled()`가 새 deadline을 만든다는 사실만으로 명세 위반이나 영속화의 필요성을 확정하지 않는다.
저장된 실패 횟수와 시도별 제한을 함께 검사해야 한다. 기존 재시도 구현은 유지하며 다음을 검증한다.

1. 같은 입력 버전의 실제 호출 횟수·시도별 대기·전체 대기가 명세 한도를 지키는가.
2. 실패 기록 전후의 중단·재시작에서도 최신 입력·응대 주체·이미 적용한 결과를 확인하는가.
3. 중단된 호출의 시도 기록과 재기동 후 재처리 정책이 한도·중복 방지 요구사항을 만족하는가.
4. 서버 중단 시간이 전체 대기 예산에 포함되는지 등 불명확한 시간 계산 기준을 팀과 확인했는가.

규약 위반을 재현한 경우에만 근본 원인에 맞춰 최소 보완한다. 영속화·추가 DB 테이블·AI 측 카운터를
미리 필수 개발로 지정하지 않는다. 재시도 책임은 계속 Spring 한 곳에 둔다.

## 실제 통합 합격 조건과 증빙

승인된 합성 문의·테스트 업체를 사용한다. 성공한 기존 QA 증빙은 유지하고 아래 미확인·변경
항목만 추가 확인한다. 실제 환경 접근·변경 권한이 제공되기 전에는 완료로 표시하지 않는다.

| 항목 | 실제 환경에서 남길 증빙 | 현재 범위 |
| --- | --- | --- |
| 작업 유형·제공 범위 불일치 | 사장님 전달 결과·OWNER_CHECK·알림 저장, 잘못된 분류만으로 상담이 닫히지 않음 | AI 자동 검사, Dev 대기 |
| 자동 예약 비활성 | 제공 작업이 서비스 불가가 아닌 OWNER_CHECK로 저장 | AI 자동 검사 완료, Dev 대기 |
| 고객 유형 수정 | 기존 계약에서 DB 수정값 보존·최신 버전 적용, 불필요한 재질문·잘못된 서비스 불가 안내 여부 | 기존 계약으로 통합 검증, 불일치 시 연동 방식 협의 |
| 사장님 확인 후 재개 | OWNER 확인값·새 버전 전달, 해결한 항목 재질문 없음, 안전/필수 미해결 재개 차단 | AI 자동 검사 완료, 실제 화면·DB 대기 |
| AI 재시작 | 같은 최신 요청으로 재기동 전후 판단 유지, 답변 한 건만 저장 | 별도 프로세스 검사 완료, Dev 대기 |
| Spring 중단·재시작 | DB 최신 상태로 미완료 작업 복구, 성공한 메시지·예약 재생성 없음 | 실제 DB·복구 작업 대기 |
| 실패·시간 초과 | 실제 시도 수 최대 2회, 시도별 30초/전체 60초, 권한·할당량 1회 후 종료 | AI 대역 검사·Spring 소스 확인, 실제 설정·시험 대기 |
| 재시작 전후 재시도 | 중단 전후에도 명세의 호출·시간 한도와 중복 방지를 지키는지 확인 | 검증 항목, 위반 확인 후 보완 여부 결정 |
| 오래된 응답 | 새 입력·OWNER 전환·재개 후 이전 응답은 STALE, 필드·사진 이력·답변 불변 | 기존 WBS 검증 기록 유지, AI 모의 회귀 검사 |
| 예약 멱등성 | 같은 Idempotency-Key+요청은 기존 결과, 다른 요청 지문은 409 | 후속 예약 WBS 연계, 실제 예약 API 대기 |

2026-11-19 당시 실호출: 로컬 FastAPI ASGI 경로와 실제 Bedrock으로 **14개 사례가 모두 통과**했다.
이 중 정상 추론 9회, 잘못된 모델 ID를 사용한 AWS 오류 요청 1회, 모델 호출 없는 우선 처리·거부
4건이다. 미제공 작업·현재 유형 우선 적용·참고정보 미확인 진행·OWNER 값 재사용·새 AI 프로세스의
동일 상태 재사용·가스 신고와 부정 표현·실제 모델 오류의 단일 호출을 확인했다.
이는 미합의 확장을 포함했던 `intake-ai05-v2`의 과거 기록이다. 현재 코드의 실호출 완료 근거로
그대로 사용하지 않는다. 11/20 도구는 확장 사례를 제외한 13개 사례를 사용했고
OWNER 사례도 실제 고객 대화에 수리 요청을 포함하도록 변경했다. 그 시점의 v3는 실호출하지 않았다.
11/25 현재 도구는 여기에 부품 교체·누수 수리·본체 교체·대상 불명 4개를 추가해 17개를 사용한다.
새 4개 사례는 서버 보정 후 결과뿐 아니라 실제 모델의 원시 유형·확인 상태도 맞아야 통과한다.
현재 v5·규칙 v3의 최종 실호출 17/17 결과는 아래 11/25 기록을 따른다.

API 요청·결과의 식별자는 비교하되 원문·비밀키·서명 URL·개인정보를 증빙에 넣지 않는다.
공유 Dev 재시작·설정 변경·실제 예약 저장은 영향이 있는 작업이므로 사전 승인 후 수행한다.

## 검증 명령

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_ai05_routing.py tests/test_ai05_recovery.py tests/test_intake_bedrock.py tests/test_intake_deadline.py tests/test_ilro_multiturn.py -p no:cacheprovider --tb=short --basetemp=artifacts/ai05-targeted-tmp
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --tb=short --basetemp=artifacts/ai05-full-tmp
.\.venv\Scripts\ruff.exe check
.\.venv\Scripts\ruff.exe format --check
git diff --check
```

실제 실행 결과는 아래 검증 기록에 추가한다. 외부 Bedrock·Dev·실제 Spring DB 시험과 구분한다.

### 2026-11-19 실행 결과

- 새 분기·복구·기존 질문 정책 관련 5개 파일 검사: **92개 통과**.
- 위 검증 명령의 AI-05·Bedrock·시간 제한·멀티턴 대상 검사: **98개 통과**.
- 위 전체 pytest 명령: **569개 통과**.
- `ruff check`, `ruff format --check`, `git diff --check`: 모두 통과.
- `git apply --check --directory=artifacts/ai05/backend-baseline docs/patches/ai05-current-work-category.patch`:
  당시 백엔드 원본 사본에 적용 가능성 검사 통과. 원격 저장소에는 적용하지 않았다.
  11/20에는 패치를 보류 문서로 옮겼으므로 이 명령은 현재 적용 절차가 아니다.
- 새 프로세스 검사는 실제 별도 Python 프로세스에서 FastAPI를 실행했다. Bedrock은 SDK 대역,
  Spring 결과 적용은 메모리 모사이며 외부 서비스 호출·과금·실제 DB 변경은 없었다.
- 공유 Dev의 접속 경로·테스트 계정·재시작 권한이 없어 실제 통합 합격 조건은 미완료다.
  위 기록은 AI-05 전체 완료 선언이 아니다.

### 실제 Bedrock 추가 검증

사용자가 `ilro-dev` 프로파일을 통한 실제 모델 검증을 요청해 실행했다.
위 기존 기록 이후 프롬프트·구조화 필드 설명을 보완하고, 실검증 도구와 판정기 테스트를 추가했다.
당시 전체 자동 테스트는 **582개 통과**, Ruff 린트·포맷과 `git diff --check`도 통과했다.
이 자동 테스트 수치에는 실제 AWS 호출을 포함하지 않는다. 별도 실호출의 명령·14개 사례 결과·
초기 실패 기록·모델 사용량은 [AI05_bedrock_live_20261119.md](AI05_bedrock_live_20261119.md)를 참조한다.

### 2026-11-20 기존 API 기준 범위 정리·검증

- `currentWorkCategory` 수용·모델 입력·그래프 우선 적용·모의 저장을 제거했다.
  기존의 알 수 없는 요청 필드 거부에 따라 해당 필드를 보내면 400 `INVALID_INPUT`이며 모델은 호출하지 않는다.
- 서비스 불가 분기·필수/참고정보 분리·안전 우선 처리·OWNER 값 보호와 기존 모델 출력 검증을 유지했다.
- 재시도 영속화는 위 검증 목록으로 전환했다. 기존 Spring 재시도 코드·DB·실행 설정을 바꾸지 않았다.
- 요청·응답·업체·기존 항목 모델의 필드 목록을 Git HEAD의 기존 계약과 비교했다.
  `AnalyzeRequest` 12개, `AnalyzeResult` 20개, `Business` 2개, `ExistingField` 7개가 각각 동일했다.
  이는 해당 모델의 필드 목록 비교이며 API 명세 전체 검증이나 실제 Spring 통합 검증을 뜻하지 않는다.
- 관련 5개 테스트 파일 **84개 통과**. 전체 자동 테스트 **581개 통과**.
  확장 전용 검사를 기존 계약 검사로 변경했으므로 11/19의 582개와 테스트 구성이 다르다.
- 최초 샌드박스 전체 검사는 하위 프로세스 대기로 진행되지 않아 중단했다.
  권한이 허용된 환경에서 아래 전체 명령을 재실행해 통과했다. 실제 AWS·Dev·DB에는 접속하지 않았다.
- 프로젝트 Python 소스 76개에 대한 Ruff 린트·포맷과 `git diff --check` 통과.
  전체 디렉터리 탐색은 테스트 임시 폴더 접근 오류 및 Ruff 포맷 검사 크래시가 발생해,
  생성물 `artifacts/`·`results/`를 제외한 실제 소스 파일을 명시해 최종 검사를 마쳤다.
  과거 실호출 JSON은 수정하지 않았다.
  `intake-ai05-v3`의 변경된 13개 사례는 이번에 실호출하지 않았으므로 새 실호출 통과를 주장하지 않는다.

실제로 실행한 검증 명령:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_ai05_routing.py tests/test_ai05_recovery.py tests/test_ai05_live_checks.py tests/test_ai02_conversation.py tests/test_ilro_multiturn.py -p no:cacheprovider --tb=short --basetemp=artifacts/ai05-contract-scope-tmp
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --tb=short -o faulthandler_timeout=60 --basetemp=artifacts/ai05-scope-full-permitted-tmp
$sourcePaths = @(rg --files -g '*.py' -g '!artifacts/**' -g '!results/**' -g '!.venv/**')
.\.venv\Scripts\ruff.exe check --no-cache -- $sourcePaths
.\.venv\Scripts\ruff.exe format --check --no-cache -- $sourcePaths
git diff --check
```

다음 통합 검증은 기존 계약으로 고객 유형 수정 후 안내·저장값이 일치하는지, 사장님 응대 재개·
서버 복구·재시도 한도를 만족하는지 확인하는 것이다. 불일치가 재현되기 전에는 API 확장이나
재시도 영속화를 추가 필수 개발로 지정하지 않는다.

### 2026-11-23 이슈 #16 커밋 범위 분리 검증

원격 develop과 기존 QA 브랜치의 기준 파일 트리가 같은 것을 확인한 뒤
`feat/#16-langgraph-routing-safety` 브랜치에서 AI-05 관련 15개 파일만 선별했다.
`.env.example`의 기존 변경, AI-01 실험 코드·테스트, 모델·샘플·실행 결과와 로컬 참조용 원본 문서는 제외했다.

Git 인덱스의 파일만 `artifacts/ai05-pr-review-20261123/`에 추출해 독립적으로 검사했다.
이 폴더의 전체 자동 테스트 **482개**, Python 소스 71개의 Ruff 포맷·린트가 통과했다.
11/20의 581개는 개인 미추적 테스트까지 포함한 로컬 결과이며 이번 커밋 범위의 테스트 수와 다르다.
선별된 파일 목록과 Git 공백 검사, 자격 증명 패턴 검사도 통과했다.
이는 로컬 분리 검증이며 GitHub CI·Dev 배포·실호출 검증 완료를 뜻하지 않는다.

### 2026-11-25 PR #17 피드백 반영·실검증

팀원 피드백의 세 항목을 반영했다. API 요청·응답 필드와 enum은 유지했고,
새 API 확장·재시도 영속화·Dev 상태 변경을 추가하지 않았다.

1. 제공 범위와 모델 유형이 다르면 `UNSERVICEABLE` 대신 `ESCALATE_TO_OWNER`를 반환한다.
   기존 Spring의 OWNER_CHECK·사장님 알림 경로를 사용하도록 했으며 실제 Dev 알림 저장은 별도 검증이다.
   고객 정정 후 최신 입력으로 다시 분석할 수 있고 모델 오분류만으로 상담을 닫지 않는다.
2. REPAIR=수리·부품 교체, LEAK=누수 수리, REPLACEMENT=보일러 본체 교체로 추출 지시·필드 설명을 보완했다.
   교체 대상 검증을 추가해 잘못된 본체 교체 후보를 보정하거나 미확인으로 둔다.
   유형 확인 질문도 부품/본체를 구분하고, 누수 증상 질문은 물샘 위치 등을 묻도록 했다.
3. 기존 계약의 13개 사례에 부품 교체·누수 수리·본체 교체·대상 불명 4개를 추가했다.
   현재 프롬프트와 규칙으로 실제 Bedrock을 호출하고 원시 유형·서비스 응답을 함께 검사했다.

실호출 과정에서 확인한 실패 기록도 보존했다.

| 실행 | 버전 | 결과 | 확인한 문제 |
| --- | --- | --- | --- |
| 첫 실행 | intake-ai05-v4 / rules-ai05-v2 | 16/17 | 누수 유형은 맞았으나 symptom 추출 누락 |
| 두 번째 | intake-ai05-v5 / rules-ai05-v2 | 16/17 | symptom은 추출했지만 서버가 `배관에서 물이 샘`을 미확인으로 처리 |
| 최종 | intake-ai05-v5 / rules-ai05-v3 | **17/17** | 명사형 물샘 인정 후 모든 검사 통과 |

두 번째 실패는 실제 원시 값과 그래프 출력을 대조해 증상 판별 규칙의 표현 누락으로 확인했다.
`물이 샘`이라는 정상적인 정규화 표현을 인정하도록 최소 수정했고, 부정·불확실 표현과
`물이 샘플입니다` 같은 다른 단어는 확인 근거로 쓰지 않는 회귀 검사도 추가했다.

최종 실호출은 **2026-11-25 17:17:17~17:18:22 KST**에 수행했다.
모델은 `global.anthropic.claude-haiku-4-5-20251001-v1:0`, 리전은 `ap-northeast-2`다.
정상 추론 12회·잘못된 모델 ID 오류 요청 1회·모델 호출 없는 우선 처리/거부 4건을 포함한다.

| 추가 사례 | 모델 원시 유형/상태 | 서비스 동작 |
| --- | --- | --- |
| 밸브 교체 | REPAIR / CONFIRMED | CHECK_SCHEDULE |
| 배관 누수 수리 | LEAK / CONFIRMED | CHECK_SCHEDULE |
| 보일러 본체 전체 교체 | REPLACEMENT / CONFIRMED | CHECK_SCHEDULE |
| 대상 없는 교체 요청 | null / UNCONFIRMED | ASK_MORE_INFO, work_category 질문 |

모든 결과의 입력 버전·요청 ID·출력 스키마·모델 호출 수·사용량을 검사했다.
최종 보고서의 모든 소스 해시가 현재 구현과 일치함도 확인했다. 한 번의 고정 사례 실행 결과이며
모든 한국어 표현의 정확도나 실제 Spring·프론트 통합 완료를 뜻하지 않는다.

- 최종 결과: `results/ai05_bedrock_live_20261125T081822208037Z.json`.
- 첫/두 번째 실패: `results/ai05_bedrock_live_20261125T080929759763Z.json`,
  `results/ai05_bedrock_live_20261125T081434741491Z.json`.
- 최종 SDK 보고 사용량: 입력 48,310·출력 2,061토큰. 세 실행 합계는 입력 143,706·출력 6,299토큰,
  Converse 시도 39회(정상 추론 36회·오류 요청 3회)다. 실제 청구액을 계산한 값은 아니다.
- 물샘 수정 관련 검사 220개 통과, 최종 전체 로컬 자동 테스트 **609개 통과**.
  자동 테스트는 SDK 대역을 사용하며 실제 모델 호출과 구분한다.
- PR에 포함할 파일만 Git 인덱스에서 `artifacts/ai05-pr17-review-20261125/`로 분리해
  전체 자동 테스트 **510개 통과**, Python 소스 73개의 Ruff 린트·포맷 검사도 통과했다.
  로컬 609개에는 이번 PR 범위 밖의 개인 미추적 테스트 99개가 포함되어 있다.
  선별된 15개 파일의 공백 검사·자격 증명 패턴 검사와 `.env` 계열 제외도 확인했다.
- 실제 Spring DB·Dev 배포·재시작·사장님 알림의 통합 확인은 수행하지 않았다.

실제 실행한 최종 검증 명령:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --tb=short -o faulthandler_timeout=60 --basetemp=artifacts/ai05-review-20261125-final-full
.\.venv\Scripts\python.exe -X utf8 check_ai05_bedrock_live.py --profile ilro-dev --expected-account "<ACCOUNT_ID>"
.\.venv\Scripts\ruff.exe check app tests check_ai05_bedrock_live.py simulate_ilro_multiturn.py
.\.venv\Scripts\ruff.exe format --check app tests check_ai05_bedrock_live.py simulate_ilro_multiturn.py
git diff --check
```
