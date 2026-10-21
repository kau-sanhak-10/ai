# 일로 AI 서버

고객 대화·사진에서 접수정보를 추출하고, 문의 상황을 판단(Jev·범용 LLM·자체 모델 비교)하는 FastAPI 서버입니다.
현재는 개발 환경, 공통 응답·오류 처리, 헬스체크만 준비되어 있습니다. 대화·Vision·판단 기능은 아직 구현되지 않았습니다.

## 시작하기

Python과 패키지는 [uv](https://docs.astral.sh/uv/)로 관리합니다. uv가 `.python-version`의 Python 3.12를 자동으로 내려받으므로 Python을 따로 설치하지 않아도 됩니다.

```powershell
# uv 설치 (최초 1회) — 설치 후 터미널을 새로 여세요
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

git clone https://github.com/kau-sanhak-10/ai.git
cd ai
uv sync
Copy-Item .env.example .env
uv run uvicorn app.main:app --reload --port 8000
```

macOS·Linux는 `curl -LsSf https://astral.sh/uv/install.sh | sh`로 설치하고, `Copy-Item` 대신 `cp`를 사용하세요.

- API 문서: http://localhost:8000/docs
- 헬스체크: http://localhost:8000/health → `{"status": "UP"}`

## 기본 구성

| 위치                     | 역할                                                    |
| ------------------------ | ------------------------------------------------------- |
| `app/main.py`            | FastAPI 앱 생성·라우터 등록                             |
| `app/core/config.py`     | `.env`·환경변수 설정. API 키는 `SecretStr`로 읽음       |
| `app/common/response.py` | 백엔드와 같은 `{success, code, message, result}` 응답   |
| `app/common/errors.py`   | 오류 코드(`ErrorCode`)와 전역 예외 처리                 |
| `app/api/`               | 라우터. 새 기능은 여기에 파일을 추가하고 `main.py`에 등록 |
| `tests/`                 | pytest 테스트                                           |

## 서비스 연결 기준

| 서비스        | 로컬 주소             |
| ------------- | --------------------- |
| 프론트엔드    | http://localhost:3000 |
| 백엔드        | http://localhost:8080 |
| AI 서버(여기) | http://localhost:8000 |

- AI 서버는 Spring Boot 백엔드만 호출합니다. 프론트엔드가 직접 호출하지 않습니다.
- LLM·Vision·Jev 키는 이 서버의 `.env`에만 둡니다. 프론트·백엔드 환경변수에 넣지 않습니다.
- 응답은 `ApiResponse.ok(result)`로 감싸 백엔드 `ApiResponse`와 같은 형식을 씁니다.
- 오류는 `raise BusinessException(ErrorCode.XXX)`로 던집니다. 공통 코드 이름·메시지는 백엔드 `CommonErrorCode`와 같습니다. AI 전용 코드는 `ErrorCode`에 추가합니다.
- 입력 검증 실패는 `INVALID_INPUT`과 함께 `result`에 `[{field, reason}]` 목록을 돌려줍니다.
- `/health`는 헬스체크용이라 `ApiResponse` 형식이 아닙니다.
- `APP_ENV=prod`에서는 `/docs`와 `/openapi.json`을 닫습니다.

## 패키지 추가

```powershell
uv add <패키지>          # 서버에서 쓰는 패키지
uv add --dev <패키지>    # 테스트·검사에만 쓰는 패키지
```

`pip install`을 쓰지 마세요. `uv add`로 추가해야 `pyproject.toml`과 `uv.lock`에 기록되고, 팀원이 `uv sync`만으로 같은 환경을 갖습니다. 두 파일은 항상 함께 커밋합니다.
모델 가중치(`*.pt`, `*.safetensors` 등)는 용량이 커서 커밋하지 않습니다. `.gitignore`에 등록되어 있습니다.

## 검사

```powershell
uv run ruff format      # 코드 서식 정리
uv run ruff check       # 린트
uv run pytest           # 테스트
```

`develop`·`main`으로 보내는 PR에서는 GitHub Actions가 같은 검사를 실행합니다.

## 팀 Git 사용 기준

- `develop`: 팀 개발 통합 브랜치, `main`: 배포 기준 브랜치입니다. 둘 다 직접 push할 수 없습니다.
- 이슈를 만든 뒤 `develop`에서 `feat/#이슈번호-설명` 형식으로 브랜치를 만들어 작업합니다.
- 커밋은 `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:` 유형을 사용합니다.
- `develop` 대상으로 PR을 열고 리뷰 1명 승인 후 Squash and merge 합니다.
- `.env`, API 키, 고객 개인정보·사진은 커밋하지 않습니다.
