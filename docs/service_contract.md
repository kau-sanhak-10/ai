# 서비스 연동 기준 · 2026-10-24

기준: [접수·인증·예약 정책서](https://app.notion.com/p/3f424a392ba98119a45ff7a5561f87dd),
[API 명세](https://app.notion.com/p/abb24a392ba982e385f6818e70436b3e).

## 현재 코드와 서비스 경계

`app/llm`은 IF-13의 제공사 접근·추출 검증 코드다. LangGraph 멀티턴이나
`/internal/v1/inquiries/analyze` 서비스 엔드포인트가 구현 완료된 것은 아니다.
`needs_review`는 추출 결과 검토 신호이며 예약 차단·사장님 이관 명령이 아니다.
`unobserved_fields`는 사진·문자에서 관측하지 못한 항목이다.
기본적으로 제조사·모델명·오류코드·주소를 공통 필수값으로 취급하지 않는다.

## DS-05 / AI-02~06 연동 완료 조건

- 서버가 작업별 필수정보와 현재 문의 상태를 판정한다. 참고정보 미확인만으로 예약을 막지 않는다.
- `model_name`은 명세의 `facts[].fieldKey=model`로 변환한다. 본체·조절기 모델을 혼동하지 않고
  확인된 근거의 messageId/photoId, source, status와 미확인 이유를 연결한다.
- `uncertainties`를 확정 사실로 저장하지 않는다. 사실·정보 충돌·매뉴얼 해석·다음 행동을 분리한다.
- 서비스 요청은 requestId/inquiryId/ownerId/inputVersion, 최신 fields/messages/photos/askCounts를 받는다.
  응답은 공통 success/code/message/result 형식과 facts/conflicts/interpretations/nextAction/reply를 따른다.
- 주소는 별도 방문정보 API에서 처리한다. 서비스 모델 입력에서 구조화된 이름·전화번호·주소를 제외하고,
  자유 대화·사진에 포함된 개인정보도 전송 전 처리 범위를 확인한다. 기존 로컬 CLI의 address 추출은
  실험용이며 실사용 전체 대화를 무가공으로 외부 API에 전송해도 된다는 뜻이 아니다.
- 문의 현재값·버전·인계 상태의 기준은 백엔드 DB다. LangGraph는 요청 내 처리를 담당하고,
  서버가 최신 상태를 전달해 재시작 후 복원한다. 지연된 결과가 사장님 수정값을 덮어쓰지 못하게 한다.
- 같은 항목의 최초 질문 후 보완은 최대 2회. 참고정보 미확인은 진행하고 필수정보 미해결은 이관한다.
  안전 우려·직접 상담 요청은 횟수와 무관하게 즉시 이관한다.
- JPG·PNG, 장당 10MB를 적용한다. 문의당 10장은 업로드 서버에서 관리한다.
  HEIC는 변환·방향·판독 검증 후 지원하며 지금은 지원한다고 표시하지 않는다.

이 문서는 연동 작업의 승인 기준이다. 위 항목의 서비스 구현·통합 검증은 해당 WBS에서 별도로 완료 판정한다.
