# 사람 검토 API

## 검토 항목 생성

Workflow는 민감 실행 의도, 근거 부족, 문서 충돌, 관련성 출력 오류, 답변 Schema 오류, 허용되지 않은 인용, 민감 출력이 발견되면 작업을 `REVIEW_REQUIRED`로 바꾸고 `review_queue`에 같은 트랜잭션으로 기록한다.

초안 인용과 승인 가능한 인용은 분리해 저장한다. 승인 가능한 인용에는 이번 실행에서 관련성이 통과된 Chunk만 포함된다. 모델이 만든 임의 ID는 수정 승인으로도 추가할 수 없다.

## 조회

- `GET /api/v1/reviews?status=WAITING&limit=50&offset=0`: 상태별 목록
- `GET /api/v1/reviews/{review_id}`: 단일 항목과 원 검색 근거

## 결정

- `POST /api/v1/reviews/{review_id}/approve`: 저장된 초안과 검증된 인용을 승인한다.
- `POST /api/v1/reviews/{review_id}/edit-approve`: 검토자가 답변을 고치되 원 검색 허용목록 안의 인용만 선택한다.
- `POST /api/v1/reviews/{review_id}/retry`: 문서 보완 뒤 같은 작업을 새 Workflow 세대로 재검색한다.
- `POST /api/v1/reviews/{review_id}/reject`: 근거 부족 등의 이유로 작업을 반려한다.

각 결정은 `WAITING` 행을 `FOR UPDATE`로 잠그고 작업 상태를 함께 조건부 변경한다. 같은 결정을 재전송하면 기존 결과를 성공으로 반환하며, 이미 다른 결정이 내려진 항목은 `409`를 반환한다.

## 안전 경계

초안이나 허용된 인용이 없는 항목은 승인 또는 수정 승인할 수 없다. 문서를 보완하고 재검색하거나 반려해야 한다. 현재 API는 로컬 개발용으로 인증이 없으며, 운영 노출 전 인증·권한·감사 주체 기록이 필요하다.
