# Slack RAG Harness

무료·로컬 실행을 우선하는 비동기 AI 하네스다. 현재 구현 범위는 로드맵 0단계부터 2단계까지이며, LLM·검색·실제 Slack 발신은 아직 포함하지 않는다.

## 실행

Docker Desktop과 Docker Compose가 필요하다. 호스트 Python은 필요하지 않다.

직접 의존성은 `pyproject.toml`, 해석된 전체 의존성은 `requirements.lock`에 고정돼 있다.

```powershell
Copy-Item .env.example .env
docker compose up --build -d postgres rabbitmq api outbox-publisher
docker compose ps
Invoke-RestMethod http://localhost:8000/health/ready
```

로컬 이벤트 접수:

```powershell
$body = @{ external_event_id = "local-demo-1"; question = "정산 배치 처리 방법은?" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/events -ContentType application/json -Body $body
```

반환된 `job_id`는 `GET /api/v1/jobs/{job_id}`로 조회한다. 같은 `external_event_id`를 다시 보내면 동일한 `job_id`와 `duplicate=true`가 반환된다.

## Outbox와 RabbitMQ

`outbox-publisher`는 다음 순서로 API와 RabbitMQ 사이의 유실 가능성을 줄인다.

1. PostgreSQL에서 발행 가능한 `READY` Outbox를 `FOR UPDATE SKIP LOCKED`로 선점한다.
2. DB 트랜잭션을 닫은 뒤 Publisher Confirm이 활성화된 RabbitMQ 채널로 영속 메시지를 발행한다.
3. 발행 성공 시 현재 Lease와 일치하는 Outbox만 `SENT`로 변경한다.
4. 발행 실패 시 제한된 지수 Backoff와 다음 재시도 시각을 저장한다.
5. Publisher가 중단돼 남은 오래된 `PROCESSING`은 Lease 만료 후 복구한다.

RabbitMQ에는 `rag_harness.jobs`, `rag_harness.jobs.retry`, `rag_harness.jobs.dlq` 세 Queue가 선언된다. Retry Queue는 고정 TTL 이후 기본 작업 Queue로 돌아가며, 처리 불가능한 메시지는 DLQ로 분리한다.

2단계는 메시지 전달과 중복 실행 Gate까지만 제공한다. 실제 AI Handler가 없는 임시 Consumer 서비스를 띄우면 작업을 의미 없이 소비하므로 상시 Worker 컨테이너는 아직 실행하지 않는다. 실제 Workflow Worker는 4단계에서 이 Consumer 경계에 연결한다.

## 테스트

테스트는 실제 PostgreSQL과 RabbitMQ 컨테이너를 사용하며 Slack 연결이나 유료 API가 필요 없다.

```powershell
docker compose --profile test run --build --rm test
```

개별 서비스 상태는 다음 명령으로 확인한다.

```powershell
docker compose ps
docker compose exec postgres pg_isready -U rag_harness -d rag_harness
docker compose exec rabbitmq rabbitmq-diagnostics -q ping
```

## 보안 설정

- `.env`는 Git에서 제외된다.
- `.env.example`의 값은 로컬 예시이며 실제 Slack Secret이 아니다.
- Slack 서명은 JSON 파싱 전에 원본 요청 바이트로 검증한다.
- Timestamp 허용 범위 기본값은 300초다.
- 운영 Profile에서는 `ENABLE_LOCAL_EVENTS=false`로 로컬 우회 Endpoint를 숨긴다.

요구사항 충돌과 결정 근거는 [docs/REQUIREMENTS_REVIEW.md](docs/REQUIREMENTS_REVIEW.md)에 기록했다.
