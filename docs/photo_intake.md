# C-05 / AI-03 사진 분석

Spring은 기존 내부 문의 분석 API에 `photoId`와 비공개 S3의 서명된 GET URL을 넘긴다. FastAPI는 팀 버킷에서만 JPG/PNG를 받아 로컬 YOLO로 영역을 찾고, 한국어 PP-OCRv5가 읽은 글자 위치와 대조한다. 결과는 기존 `facts`와 `analyzedPhotoIds`로 반환한다. Bedrock은 텍스트 전용이다.

- 확인되지 않은 값은 `PHOTO_UNREADABLE`이며 원본을 보관하는 주체는 Spring/S3다. 다운로드 또는 모델 실행 실패를 정상 판독으로 저장하지 않는다.
- 제조사, 보일러 본체 모델, 오류코드만 기존 계약에 맞춰 반환한다. 조절기 모델은 본체 `model`로 보내지 않는다. 숫자만 보이는 화면은 온도와 오류를 구분할 근거가 없으면 미확인이다. 참고정보 미확인만으로 예약을 막지 않는다.
- Spring이 사진만 보낸 메시지를 `content=""`로 보내는 경우에도 연결된 사진이 있으면 받는다. 텍스트 발화가 없는 사진 요청은 Bedrock을 호출하지 않는다.
- Spring의 사진 처리 이력 연동은 저장된 성공 응답의 `analyzedPhotoIds`를 다음 요청의 `processedPhotoIds`로 넘긴다. FastAPI는 해당 사진을 다시 다운로드하거나 분석하지 않는다. 실패하거나 늦게 도착해 적용하지 못한 응답은 처리 이력에 넣지 않는다.
- 고객 문장과 새 사진이 함께 오면 Bedrock 텍스트 분석과 로컬 사진 분석을 동시에 시작한다. 여러 사진은 메모리 사용을 제한하기 위해 순차 처리한다. 전체 분석에는 25초 제한을 적용한다.

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
- Dev에는 이 버전의 FastAPI를 먼저 배포하고 Spring의 사진 이력 연동을 배포한다. 이전 FastAPI는 `processedPhotoIds`를 허용하지 않는다. 사진 분석만 중단해야 하면 새 FastAPI 버전을 유지한 채 사진 환경값을 비운다. 이전 FastAPI로 되돌려야 하면 Spring부터 되돌린다.
- Dev 테스트 문의에서 ① 글 없이 사진 첨부 ② 사진과 증상 문장 동시 전송 ③ 다음 메시지와 새 사진 전송 순으로 검증한다. 고객 응답 시간과 FastAPI 분석 시간을 각각 기록하고, Spring에 저장된 사진 근거 ID가 맞는지, 사장님 화면에서 원본과 추출 정보가 함께 보이는지, 다음 턴에 이미 처리한 사진이 반복 분석되지 않는지 확인한다. 요청 시간과 메모리를 측정하기 전에는 운영 설정을 켜지 않는다.

## 로컬 검증 · 11/10

2 vCPU로 제한한 Linux 컨테이너에서 S3 다운로드만 사진 파일로 대체하고, YOLO·OCR과 FastAPI 응답은 실제로 실행했다. 기존 샘플 사진 10장 모두 HTTP 200과 올바른 사진 근거 ID를 반환했다. 워밍업 이후 2.5~24.0초, 중앙값 약 9.8초, 컨테이너 최대 메모리 약 834 MiB였다. 제조사 7건과 본체 모델 문자열 1건을 출력했으며, 모델 문자열은 글자 오독이 있었고 이 표본의 오류코드는 확정하지 못했다. 독립 정확도 평가가 아니며, 실제 S3·Bedrock·Spring을 합친 요청은 검증하지 않았다. 사진 분석만으로 24초에 근접한 사례가 있어 Dev에서 전체 시간 제한을 먼저 확인한다.

모델 출처: [Ultralytics YOLOE](https://docs.ultralytics.com/models/yoloe/) · [PaddleOCR 경량 글자 탐지](https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_det)
