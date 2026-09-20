# 구현 진행 기록

## 2026-09-19 — 0단계·1단계 완료

### 현재 단계

- 0단계 환경과 뼈대: 완료
- 1단계 작업 접수와 멱등성: 완료
- 2단계 이후: 미구현(사용자 요청 범위 밖)

### 요구사항 검토

- Slack `url_verification`도 원본 Body 서명 검증 후 challenge를 반환하도록 처리 순서를 보정했다.
- 1단계는 Slack 수신 계약·서명·Fixture 저장, 7단계는 실제 Slack 발신·Tunnel·Workspace E2E로 중복 범위를 분리했다.
- 기존 Thread 안의 멘션은 `event.thread_ts`, 최상위 멘션은 `event.ts`를 회신 Thread 대상으로 저장하도록 보정했다.
- Checkpointer 단독으로 장애 복구가 보장되지 않고 ACK·Lease 설계가 함께 필요하다는 후속 단계 제약을 기록했다.
- 상세 결정은 `docs/REQUIREMENTS_REVIEW.md`에 기록했다.

### 완료 항목

#### 0단계

- Python 3.13 기반 FastAPI 프로젝트와 Dockerfile 생성
- PostgreSQL 17 + pgvector 0.8.6, RabbitMQ 4.3.6 Compose 구성
- Liveness와 PostgreSQL·RabbitMQ 실제 연결을 검사하는 Readiness 구현
- `.env.example`, `.gitignore`, `.dockerignore`, README 작성
- 직접 의존성 `pyproject.toml`, 전체 해석 의존성 `requirements.lock`, Docker 이미지 태그 고정
- 무료 오픈소스와 라이선스 검토 기록

#### 1단계

- `ai_job`, `job_outbox`, PostgreSQL Enum, pgvector 확장 Alembic Migration 작성
- `(source, external_event_id)` DB UNIQUE 제약과 작업당 Outbox UNIQUE 제약 추가
- PostgreSQL `INSERT ... ON CONFLICT DO NOTHING RETURNING` 기반 동시 중복 처리
- 작업과 Outbox를 한 트랜잭션으로 저장하고 Outbox 실패 Rollback 검증
- `POST /api/v1/events`, `POST /api/v1/slack/events`, `GET /api/v1/jobs/{job_id}` 구현
- Slack 원본 Body HMAC, Timestamp 300초, Secret 미설정, 잘못된 서명 검증
- `url_verification`, `app_mention`, Bot 메시지 무시, Slack 재전송 Fixture 계약 테스트
- 로컬 Endpoint Profile 분리와 질문 원문 비노출 작업 조회 구현
- 모든 직접 작성 함수의 한글 Docstring과 타입 힌트를 검사하는 회귀 테스트 추가

### 실제 실행 명령과 결과

```text
docker compose config
결과: 성공

docker compose --profile test build
최초 결과: 실패 — pytest-asyncio 1.2.0이 pytest<9를 요구해 pytest 9.1.1과 충돌
조치: 호환되는 pytest-asyncio 1.4.0으로 고정하고 전체 의존성 잠금 파일 생성
재실행 결과: 성공

docker compose --profile test run --build --rm test
최종 결과: 27 passed in 1.08s

docker compose run --rm --no-deps test python -m compileall -q app tests migrations
결과: 성공

docker compose run --rm --no-deps test python -m pip check
결과: No broken requirements found.

docker compose up --build -d api
docker compose ps
결과: api, postgres, rabbitmq 모두 healthy

Invoke-RestMethod http://localhost:8000/health/ready
결과: {"status":"ok"}

docker compose exec -T postgres pg_isready -U rag_harness -d rag_harness
결과: accepting connections

docker compose exec -T rabbitmq rabbitmq-diagnostics -q ping
결과: Ping succeeded

SELECT extname || ':' || extversion FROM pg_extension WHERE extname='vector';
결과: vector:0.8.6
```

실행 중인 API에 같은 `external_event_id=manual-idempotency-check`를 두 번 전송했다.

- 첫 응답: HTTP 202, `duplicate=false`
- 둘째 응답: HTTP 202, 같은 `job_id`, `duplicate=true`
- DB 확인: 해당 `ai_job` 1건, `job_outbox` 1건

### 테스트 범위

- 정상: Health, 로컬 이벤트, Slack app_mention, URL Verification, 작업 조회
- 경계: 공백 질문, 로컬 Endpoint 비활성 Profile, 중복 재전송, 기존 Thread 대상
- 실패: 잘못된 Slack 서명, 오래된 Timestamp, Secret 미설정, Outbox PK 충돌 Rollback, 미존재 작업
- 동시성: 동일 이벤트 HTTP 요청 8건과 저장소 요청 8건이 각각 작업·Outbox 한 건으로 수렴
- 인프라: 실제 PostgreSQL 쿼리와 실제 RabbitMQ AMQP 연결
- 성능 계약: 실제 PostgreSQL 저장을 포함한 Slack Fixture ACK가 3초 미만인지 테스트

### 해결한 검증 이슈

- 최초 Python 의존성 조합의 충돌을 실제 Docker 빌드에서 발견하고 호환 버전으로 수정했다.
- 최신 Starlette의 동기 TestClient 폐기 예정 경고를 숨기지 않고 비동기 ASGI 테스트로 전환했다.
- Slack Signing Secret 기본값이 비어 있을 때 빈 키 위조 가능성을 최종 점검에서 발견해 503 안전 실패 경계를 추가했다.

### 남은 제한 사항

- Outbox Publisher와 RabbitMQ 발행·Consume·DLQ는 2단계 범위라 구현하지 않았다.
- 문서 검색, Ollama, LangGraph, 사람 검토, 장애 복구는 후속 단계 범위다.
- 실제 Slack Workspace, Cloudflare Tunnel, `chat.postMessage` E2E는 7단계 범위라 실행하지 않았다.
- Prometheus, Grafana, Loki는 8단계 범위라 아직 Compose에 추가하지 않았다.
- 현재 디렉터리는 Git 저장소가 아니어서 Git diff나 커밋 상태 검증은 수행할 수 없었다.

### 다음 작업

사용자 확인 후에만 2단계 Outbox Publisher와 RabbitMQ Queue/Retry/DLQ를 구현한다.

## 2026-09-20 — 2단계 완료

### 현재 단계

- 0단계 환경과 뼈대: 완료
- 1단계 작업 접수와 멱등성: 완료
- 2단계 Outbox와 RabbitMQ: 완료
- 3단계 이후: 미구현(사용자 요청 범위 밖)

### 설계 경계

- Outbox Publisher는 DB 선점, RabbitMQ 발행, 발행 결과 저장만 담당하도록 분리했다.
- Rabbit Consumer는 Pydantic 메시지 검증, DB 조건부 상태 전이, 중복 실행 차단을 담당한다.
- 실제 AI Workflow Handler는 4단계 범위이므로 상시 Worker 컨테이너를 임시 구현하지 않았다. 통합 테스트에서는 교체 가능한 Handler로 실제 Consume과 중복 실행 차단을 검증했다.
- RabbitMQ Retry Queue와 DLQ 전송 경계는 구성했지만, AI 오류의 일시·영구 분류와 단계별 재시도 정책은 6단계에서 연결한다.
- Outbox 발행 실패는 RabbitMQ가 없을 때도 남아야 하므로 DB의 횟수, 오류 유형, 다음 재시도 시각을 기준 상태로 사용한다.

### 완료 항목

- `FOR UPDATE SKIP LOCKED` 기반 READY Outbox 묶음 선점
- `status=READY` 조건과 Lease 시각을 함께 사용하는 조건부 상태 전이
- Publisher Confirm과 Persistent Message를 사용하는 RabbitMQ 발행
- 발행 성공 시 Outbox `SENT`, 작업 `QUEUED` 전이
- 발행 실패 시 오류 유형, 횟수, 제한된 지수 Backoff, 다음 재시도 시각 저장
- 재시도 소진 시 Outbox `FAIL`, 작업 `FAILED`, 검색 가능한 오류 코드 저장
- 오래된 `PROCESSING` Lease 복구와 복구 횟수 제한
- 작업, 고정 TTL Retry, DLQ용 Durable Exchange·Queue·Routing 구성
- Pydantic `JobMessage` 계약과 `message_id=outbox_id`, `correlation_id=job_id` 추적
- `RECEIVED/QUEUED -> PROCESSING` 조건부 UPDATE 기반 Consumer 중복 실행 Gate
- 닫힌 RabbitMQ Channel 감지 후 연결과 Topology를 새로 만드는 복구 경계
- Outbox Polling과 Lease 복구 조회용 PostgreSQL 인덱스 Migration `0002`
- Outbox Publisher Compose 서비스와 전체 환경변수 예시 추가

### 자동 테스트

```text
docker compose --profile test run --build --rm test
결과: 33 passed in 1.35s

docker compose run --rm --no-deps test python -m compileall -q app tests migrations
결과: 성공

docker compose run --rm --no-deps test python -m pip check
결과: No broken requirements found.

docker compose config --quiet
결과: 성공

git diff --check
결과: 성공
```

검증 범위:

- 정상: 실제 RabbitMQ Publisher Confirm 발행과 Consume
- 중복: 같은 메시지 두 건이 도착해도 Handler 한 번 실행
- 동시성: Publisher 두 개가 같은 Outbox를 중복 선점하지 않음
- 재시도: `next_retry_at` 전에는 다시 선점하지 않고 시각 이후에만 선점
- 소진: 최대 횟수에서 Outbox `FAIL`과 작업 `FAILED`를 함께 기록
- 복구: 오래된 `PROCESSING` Lease를 READY로 복구하고 횟수 소진 시 실패 처리
- Routing: 거절 메시지가 Retry Queue로 이동하고 오류 메시지가 DLQ에 저장
- 회귀: 1단계 Slack 서명·중복·Rollback·3초 ACK 테스트 포함 전체 33건 통과

### 실제 RabbitMQ 중단·복구 검증

실행 순서:

```text
docker compose stop rabbitmq
POST /api/v1/events
DB 상태 조회
docker compose start rabbitmq
rabbitmq-diagnostics -q ping
DB 상태 재조회
```

첫 실행에서 발견한 문제:

- RabbitMQ 복구 후에도 기존 robust channel이 닫힌 상태로 남았다.
- Publisher가 `ChannelInvalidStateError`를 반복했고 제한 횟수 5회를 소진해 Outbox `FAIL`, 작업 `FAILED`가 됐다.
- 실패 결과를 유지하고, 발행 전에 연결·Channel·Topology 상태를 검사해 닫힌 연결을 폐기하고 새 연결을 만드는 방식으로 수정했다.

수정 후 재검증:

- RabbitMQ 중단 중 API 응답: HTTP 202, `duplicate=false`
- 중단 중 DB: 작업 `RECEIVED`, Outbox `READY`, 실패 횟수 1, 다음 재시도 시각 저장
- RabbitMQ 복구: `Ping succeeded`
- 복구 후 DB: 작업 `QUEUED`, Outbox `SENT`, `sent_at` 저장
- Broker가 완전히 준비되기 전 연결 실패를 포함해 최종 `attempt_count=3`에서 성공
- Queue 선언 확인: 작업, Retry, DLQ 모두 Durable
- 검증 후 DB에서 이미 제거된 수동 테스트 작업의 고아 Queue 메시지 1건을 정리했다.

### 인프라 상태

```text
docker compose ps
결과: api, postgres, rabbitmq healthy / outbox-publisher running

GET /health/ready
결과: {"status":"ok"}

SELECT version_num FROM alembic_version
결과: 0002
```

### 남은 제한 사항

- 실제 AI Workflow Handler는 4단계에서 구현하므로 상시 RabbitMQ Worker 서비스는 아직 없다.
- Retry Queue는 Durable TTL/DLX 경로와 Routing 검증까지 완료했으며 AI 오류 분류별 재시도는 6단계 범위다.
- RabbitMQ가 재시도 횟수 소진 시점까지 복구되지 않으면 Outbox와 작업은 의도적으로 `FAIL/FAILED`에 남는다. 관리자 수동 재처리는 6단계 범위다.
- 문서 적재, Ollama Embedding, pgvector 검색은 3단계 범위라 구현하지 않았다.

### 다음 작업

사용자 확인 후에만 3단계 문서 적재와 검색을 구현한다.
