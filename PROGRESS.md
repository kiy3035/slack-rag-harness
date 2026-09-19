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
