# C-05 / AI-03 사진 분석

Spring은 기존 내부 문의 분석 API에 `photoId`와 비공개 S3의 서명된 GET URL을 넘긴다. FastAPI는 팀 버킷에서만 JPG/PNG를 받아 로컬 YOLO로 영역을 찾고, 한국어 PP-OCRv5가 읽은 글자 위치와 대조한다. 결과는 기존 `facts`와 `analyzedPhotoIds`로 반환한다. Bedrock은 텍스트 전용이다.

- 확인되지 않은 값은 `PHOTO_UNREADABLE`이며 원본을 보관하는 주체는 Spring/S3다. 다운로드 또는 모델 실행 실패를 정상 판독으로 저장하지 않는다.
- 제조사, 보일러 본체 모델, 오류코드만 기존 계약에 맞춰 반환한다. 조절기 모델은 본체 `model`로 보내지 않는다. 숫자만 보이는 화면은 온도와 오류를 구분할 근거가 없으면 미확인이다. 참고정보 미확인만으로 예약을 막지 않는다.
- Spring이 사진만 보낸 메시지를 `content=""`로 보내는 경우에도 연결된 사진이 있으면 받는다. 텍스트 발화가 없는 사진 요청은 Bedrock을 호출하지 않는다.
- 요청에 `processedPhotoIds`가 있으면 해당 사진을 재분석하지 않는다. 현재 Spring develop은 이 확장 필드를 읽거나 되돌려 주지 않고 대화 목록에 붙은 사진을 보낸다. 따라서 턴 사이 중복 방지에는 Spring 처리 이력 연동이 별도로 필요하다. 고객 메시지와 사진 분석을 합쳐 기존 요청 제한을 적용한다.

## Dev 실행

```sh
docker build --build-arg WITH_VISION=1 -t ilro-ai:vision .
```

팀 전용 저장소에 둔 모델을 컨테이너의 `/models`에 읽기 전용으로 마운트한다. 기존 텍스트·내부 인증 환경값은 유지한다.

```dotenv
PHOTO_S3_BUCKET=<Spring의 PHOTO_BUCKET과 동일>
PHOTO_S3_REGION=ap-northeast-2
PHOTO_YOLO_MODEL=/models/yoloe-26n-seg.pt
PHOTO_YOLO_PROMPTS=/models/boiler-yoloe26n-prompts.npz
PHOTO_OCR_MODELS_DIR=/models
```

- `/models`에는 위 YOLO 파일 2개와 `PP-OCRv5_mobile_det/`, `korean_PP-OCRv5_mobile_rec/` 두 모델 디렉터리가 필요하다. 모델은 외부에서 자동 다운로드하지 않는다. 시작 시 모델을 불러와 첫 실행을 마치고 요청을 받는다.
- 기본 빌드는 사진 모델 의존성을 설치하지 않는다. 사진 관련 설정을 비워 두면 기존 텍스트 경로만 동작한다.
- 현재 제공하는 YOLOE는 학습된 6클래스 모델이 아닌 영역 탐지 기준선이다. 팀 커스텀 가중치를 확보하면 동일한 클래스 이름 및 실제 사진을 검증한 후 교체하며, 이때 YOLOE 임베딩 설정은 비운다.
- Dev 테스트 문의에서 사진 업로드 → `facts[].evidence.photoId` 확인 → 같은 문의 저장·사장님 조회 → 같은 사진 중복 미처리를 순서대로 확인한다. 요청 시간과 컨테이너 메모리를 측정하기 전에는 기본 운영 설정으로 켜지 않는다.

## 로컬 검증 · 11/10

2 vCPU로 제한한 Linux 컨테이너에서 S3 다운로드만 사진 파일로 대체하고, YOLO·OCR과 FastAPI 응답은 실제로 실행했다. 기존 샘플 사진 10장 모두 HTTP 200과 올바른 사진 근거 ID를 반환했다. 워밍업 이후 2.5~24.0초, 중앙값 약 9.8초, 컨테이너 최대 메모리 약 834 MiB였다. 제조사 7건과 본체 모델 문자열 1건을 출력했으며, 모델 문자열은 글자 오독이 있었고 이 표본의 오류코드는 확정하지 못했다. 독립 정확도 평가가 아니며, 실제 S3·Bedrock·Spring을 합친 요청은 검증하지 않았다. 사진 분석만으로 24초에 근접한 사례가 있어 Dev에서 전체 시간 제한을 먼저 확인한다.

모델 출처: [Ultralytics YOLOE](https://docs.ultralytics.com/models/yoloe/) · [PaddleOCR 경량 글자 탐지](https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_det)
