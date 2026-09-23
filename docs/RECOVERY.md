# Worker 장애 복구 가이드

## 범위

6단계는 RabbitMQ가 전달한 작업을 실행하는 동안 발생한 오류를 일시 오류, 영구 오류, 사람 검토 필요로 분리한다. DB 상태를 먼저 확정한 뒤 메시지를 ACK하며, 재시도와 DLQ 발행은 PostgreSQL에 저장된 시각과 횟수를 기준으로 복구한다.

## 오류 분류

| 분류 | 대표 오류 | 상태 전이 | 처리 |
| --- | --- | --- | --- |
| 일시 오류 | Ollama timeout·HTTP 실패, PostgreSQL 연결 오류 | `PROCESSING → RETRY_WAIT` | 제한된 지수 Backoff 뒤 기존 Outbox 재발행 |
| 검토 필요 | 생성 모델의 구조화 출력 계약 위반 | `PROCESSING → REVIEW_REQUIRED` | 빈 근거의 검토 항목을 만들고 자동 재시도 중단 |
| 영구 오류 | 작업 누락, 입력·임베딩 계약 위반, 예상하지 못한 내부 오류 | `PROCESSING → DEAD_LETTER` | DB에 오류 코드를 저장하고 DLQ 발행 대기 |
| 재시도 소진 | 일시 오류가 `WORKER_MAX_ATTEMPTS`에 도달 | `PROCESSING → DEAD_LETTER` | `WORKER_RETRY_EXHAUSTED_*` 코드와 DLQ 발행 상태 저장 |

예외 메시지 원문은 DB와 로그에 저장하지 않는다. 질문·검색 문서·Secret 대신 정해진 오류 코드와 짧은 안전 문구만 남긴다.

## 재시도와 Worker 종료 복구

1. Consumer가 `RECEIVED/QUEUED` 작업을 `PROCESSING`으로 조건부 선점하고 `attempt_count`를 올린다.
2. 일시 오류이면 현재 시도 횟수로 지수 Backoff를 계산해 `RETRY_WAIT`와 `next_retry_at`을 저장한다.
3. Recovery Scheduler가 시각이 지난 작업을 `RECEIVED`로 되돌리고 고유 `job_outbox` 행을 `READY`로 다시 연다.
4. Outbox Publisher가 같은 `job_id`를 재발행하고 새 Worker는 같은 `{job_id}:run:{workflow_revision}` Checkpoint에서 재개한다.
5. Worker가 강제 종료돼 `PROCESSING`이 오래 남으면 `locked_at` Lease 만료를 감지해 같은 재시도 경로로 보낸다.
6. 최대 시도를 소진하면 `DEAD_LETTER`로 전환한다.

기본 실행 재시도는 5초, 10초, 20초처럼 증가하고 `WORKER_RETRY_MAX_SECONDS`에서 멈춘다. 긴 Ollama 실행을 정상 작업으로 유지하기 위해 기본 PROCESSING Lease는 900초다.

## DLQ와 DB 상태 연결

`DEAD_LETTER` 전이는 `dlq_attempt_count`, `dlq_next_retry_at`, `dlq_published_at`과 함께 DB에 저장된다. Recovery Scheduler는 미발행 작업을 Lease로 선점하고 원래 `JobMessage`를 재구성해 `rag_harness.jobs.dlq`에 발행한다.

RabbitMQ가 중단되면 DLQ 발행 실패 종류와 다음 시각만 저장하고 지수 Backoff로 다시 시도한다. Publisher Confirm 뒤에만 `dlq_published_at`을 기록한다. 극단적으로 Confirm 성공 직후 DB 기록 전에 Worker가 종료되면 동일 `message_id`의 DLQ 메시지가 중복될 수 있으므로 DLQ 소비자도 `job_id` 또는 `message_id`로 멱등 처리해야 한다.

## 관리자 수동 재처리

관리자 API는 기본 비활성화다. 로컬 장애 조치 중에만 `.env`에 다음 값을 설정한다.

```text
ENABLE_ADMIN_RECOVERY=true
```

`FAILED` 또는 `DEAD_LETTER` 작업만 재처리할 수 있다. 요청마다 같은 작업 안에서 고유한 `idempotency_key`를 제공한다.

```powershell
$body = @{
  idempotency_key = "incident-20260923-1"
  reason = "Ollama 모델 복구 확인"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/api/v1/admin/jobs/{job_id}/retry `
  -ContentType application/json `
  -Body $body
```

첫 요청은 작업을 `RECEIVED`로 바꾸고 `workflow_revision`을 한 번 올리며 기존 Outbox를 `READY`로 연다. 같은 키의 중복 요청은 세대를 다시 올리지 않고 `idempotent=true`를 반환한다. 요청 이력은 `job_recovery_request`에 남는다. 이 Endpoint는 로컬 운영 보조용이며 외부 공개 전 인증·권한 계층을 추가해야 한다.

## 장애 시나리오와 기대 상태

| 시나리오 | 기대 DB 상태 | 복구 확인 |
| --- | --- | --- |
| Ollama 종료 | `RETRY_WAIT`, `OLLAMA_*`, 다음 재시도 시각 | Ollama 복구 뒤 Outbox 재발행과 Checkpoint 재개 |
| RabbitMQ 종료 | Outbox 또는 DLQ 발행 시도와 다음 재시도 시각 보존 | RabbitMQ 복구 뒤 Publisher Confirm과 상태 갱신 |
| PostgreSQL 일시 중단 | 메시지를 ACK하지 않고 Worker 재시작·재전달 허용 | DB 복구 뒤 조건부 선점 |
| Worker 강제 종료 | 만료 전 `PROCESSING`, 만료 후 `RETRY_WAIT` | 같은 Workflow 세대 Checkpoint 재개 |
| 잘못된 모델 JSON | `REVIEW_REQUIRED`, `MODEL_OUTPUT_INVALID` | 검토 큐 한 건 생성, 무한 재시도 없음 |
| 메시지 중복 전달 | 첫 메시지만 `PROCESSING` 선점 | 후속 메시지는 Handler 실행 없이 ACK |

## 주요 설정

- `WORKER_PROCESSING_LEASE_SECONDS`
- `WORKER_MAX_ATTEMPTS`
- `WORKER_RETRY_BASE_SECONDS`
- `WORKER_RETRY_MAX_SECONDS`
- `WORKER_RECOVERY_BATCH_SIZE`
- `WORKER_RECOVERY_POLL_SECONDS`
- `DLQ_PUBLISH_RETRY_BASE_SECONDS`
- `DLQ_PUBLISH_RETRY_MAX_SECONDS`
- `ENABLE_ADMIN_RECOVERY`

자동 테스트는 실제 PostgreSQL과 RabbitMQ를 사용하되 Ollama 중단은 결정적 Fake Handler로 재현한다. 외부 유료 서비스나 실제 회사 데이터는 사용하지 않는다.
