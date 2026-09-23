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

## 2026-09-20 — 3단계 완료

### 현재 단계

- 0단계 환경과 뼈대: 완료
- 1단계 작업 접수와 멱등성: 완료
- 2단계 Outbox와 RabbitMQ: 완료
- 3단계 문서 적재와 검색: 완료
- 4단계 이후: 미구현(사용자 요청 범위 밖)

### 2단계 병합

- 검증된 `feat/stage-2-outbox-rabbitmq`를 원격 브랜치에 보존했다.
- `gh` CLI가 설치돼 있지 않아 로컬 `main`에 명시적인 병합 커밋을 만든 뒤 GitHub `main`에 푸시했다.
- 병합 커밋: `e67e23b Merge stage 2 outbox and RabbitMQ pipeline`
- 병합된 `main`에서 `feat/stage-3-retrieval` 브랜치를 생성했다.

### 설계 경계

- Ollama는 호스트에서 실행하는 무료 로컬 서비스이며 기본 Embedding 모델은 `nomic-embed-text`다.
- `vector(768)`은 Migration과 설정 검증 양쪽에서 고정했다. 다른 차원 모델은 새 Migration 없이 사용할 수 없다.
- 외부 임베딩 호출 중에는 DB 트랜잭션을 열어두지 않는다.
- 같은 `source_path`의 교체 트랜잭션은 PostgreSQL advisory lock으로 직렬화하고, 트랜잭션 안에서 해시를 다시 확인해 동시 재적재에도 중복 Chunk가 생기지 않게 했다.
- 원문과 제목의 SHA-256이 같으면 임베딩 호출과 DB 쓰기를 생략한다.
- 문서가 바뀌면 버전을 올리고 이전 Chunk를 삭제한 뒤 새 Chunk 전체를 같은 트랜잭션에 저장한다. 검색은 현재 버전만 대상으로 한다.
- 최소 검색 점수 기본값은 평가 없이 임의의 정답을 만들지 않도록 `-1.0`이며 설정으로 외부화했다.

### 완료 항목

- `knowledge_document`, `knowledge_chunk`와 HNSW cosine index Migration `0003`
- 제목 경계 우선, 긴 섹션 1,200자·120자 중첩 Markdown 분할 규칙
- Pydantic 문서·Chunk·Ollama 요청/응답·검색 결과 Schema
- Ollama `/api/embed` Client와 timeout, HTTP, JSON 계약, 개수, 768차원, 유한값 검증
- pgvector cosine Top-K 검색, 최소 점수, 문서별 최대 Chunk 수 제한
- 문서 버전, 동일 해시 생략, 변경 버전 원자적 교체 정책
- 정산, 배포 롤백, 접근 권한, 장애 대응, 고객 공지, 백업 복구 가상 매뉴얼 6개
- 실제 Ollama를 사용할 수 있는 문서 적재·검색 CLI
- 통합 테스트 종료 시 Fake 임베딩과 작업 데이터를 항상 제거하는 Fixture 정리
- 실행 방법과 모델 차원 제약을 `docs/RETRIEVAL.md`에 기록

### 자동 테스트

```text
docker compose --profile test run --build --rm test
최종 결과: 41 passed in 1.52s

docker compose --profile test run --rm --no-deps test sh -c "python -m compileall -q app tests && pip check"
결과: 컴파일 성공, No broken requirements found.

docker compose config --quiet
결과: 성공

git diff --check
결과: 성공
```

검증 범위:

- 정상: 제목별 Markdown 분할, Ollama 정상 응답, 실제 PostgreSQL 저장과 pgvector cosine 검색
- 경계: 긴 섹션 최대 길이 분할, 빈 Markdown 거부, 현재 문서 버전만 검색
- 실패: Ollama timeout 전용 오류 변환, 필수 필드가 없는 응답 차단, 잘못된 개수·차원·비유한값 차단
- 멱등성: 동일 문서 두 번째 적재에서 Embedding Client를 호출하지 않고 Chunk 수 유지
- 버전: 변경 문서 재적재 시 버전 2로 증가하고 이전 버전 Chunk가 남지 않음
- 검색 평가: 대표 질문 10개 모두 기대한 가상 매뉴얼이 Top-3에 포함
- 회귀: 0~2단계 Slack 서명, DB 멱등성, Outbox, 실제 RabbitMQ 테스트를 포함한 전체 41건 통과
- 격리: 최종 테스트 후 `knowledge_document=0`, `knowledge_chunk=0`

### 해결한 검증 이슈

- Embedding 차원을 `Literal[768]`로 처음 제한했을 때 Compose 환경변수 문자열 `"768"`을 Pydantic이 변환하지 못해 Migration 시작 전에 실패했다. 문자열 변환과 768 고정을 함께 만족하는 `Field(ge=768, le=768)` 제약으로 수정하고 전체 테스트를 재실행했다.
- 최초 통합 테스트 Fixture가 테스트 시작 전에만 테이블을 비워 결정적 Fake 임베딩 6개 문서가 개발 DB에 남았다. `finally`에서 다시 비우도록 수정했고 최종 테스트 후 문서와 Chunk가 모두 0건임을 실제 쿼리로 확인했다.

### 실제 인프라 검증

```text
docker compose up --build -d api outbox-publisher
결과: api healthy, outbox-publisher running, postgres/rabbitmq healthy

GET /health/ready
결과: {"status":"ok"}

SELECT version_num FROM alembic_version
결과: 0003

SELECT extversion FROM pg_extension WHERE extname='vector'
결과: 0.8.6
```

- `knowledge_chunk`에 HNSW cosine index와 문서 버전 index가 실제 생성됐음을 확인했다.
- 최초 검증 시점에는 호스트의 `ollama` 명령이 설치돼 있지 않아 실제 모델 품질 검증을 실행하지 못했다. 이후 설치 후 검증 결과는 아래 후속 기록에 추가했다.
- Ollama가 없어도 자동 테스트는 결정적 Fake Client와 HTTP Mock으로 전부 통과하며 유료 API나 Slack 연결을 사용하지 않는다.

### 남은 제한 사항

- 대표 질문 10개 평가는 소규모 가상 매뉴얼 기준이며 더 큰 한국어 문서 집합의 검색 품질을 보장하지 않는다.
- 검색어 재작성, LLM 관련성 판정, 답변 생성은 4단계 이후 범위다.
- 실제 AI Workflow Worker가 없으므로 RabbitMQ 작업과 검색 서비스를 연결하지 않았다.

### 다음 작업

사용자 확인 후에만 4단계 LangGraph 기본 Workflow를 구현한다.

## 2026-09-20 — 실제 Ollama 후속 검증

### 환경

- 모델: `nomic-embed-text:latest`
- 모델 digest: `0a109f422b47`
- 크기: 274 MB
- 파라미터: 137M, F16
- Embedding 차원: 768
- 저장소: PostgreSQL pgvector 0.8.6, `vector(768)`

### 실제 실행에서 발견한 문제와 수정

- 접두어 없이 실제 모델을 처음 실행했을 때 대표 질문 10개 중 9개만 기대 문서가 Top-5에 포함됐다.
- “릴리스 이전 이미지로 되돌린 뒤 무엇을 검증하나요?” 질문은 기대한 배포 롤백 문서가 Top-5에서 누락됐다.
- Nomic 공식 모델 카드에서 RAG 문서는 `search_document:`, 질문은 `search_query:` 접두어가 필수임을 확인했다.
- `EmbeddingTask` Enum으로 문서와 질문을 구분하고 Ollama Client가 접두어를 자동 적용하도록 수정했다.
- 모델과 접두어 조합을 문서 인덱스 해시에 포함해 전처리 계약이 바뀌면 같은 원문도 자동으로 재임베딩되게 했다.
- 단위 테스트가 HTTP JSON을 Unicode escape로 가정해 1건 실패한 문제를 실제 UTF-8 JSON 파싱 방식으로 수정했다.
- 통합 테스트 DB가 개발 DB와 같아 실제 적재 문서가 삭제되는 문제를 확인했다. Compose 테스트 Profile이 `rag_harness_test`를 자동 생성·사용하도록 분리했다.

### 최종 실제 모델 결과

```text
실제 nomic-embed-text 문서 적재: 6 documents, 24 chunks, 모두 version 1
동일 문서 재적재: 6개 모두 changed=false
대표 질문 검색: 10/10 통과
기대 문서 순위: 10개 질문 모두 1위
```

질문 범위:

- 정산 마감과 원장 불일치 재처리 2개
- 배포 롤백 조건과 검증 2개
- 접근 권한 신청과 퇴사자 계정 회수 2개
- P1 장애 에스컬레이션 1개
- 고객 공지 제외 정보 1개
- 백업 복구 훈련과 쓰기 재개 조건 2개

### 회귀 검증

```text
docker compose --profile test run --build --rm test
접두어와 테스트 DB 격리 수정 후 최종 결과: 46 passed in 1.66s

docker compose up --build -d api outbox-publisher
결과: 새 접두어 코드로 이미지 재생성 및 서비스 기동 성공
```

- 실제 모델로 적재한 6개 문서와 24개 Chunk는 로컬 개발 DB에 유지했다.
- 이후 테스트는 별도 `rag_harness_test` DB만 초기화하므로 개발 DB의 실제 임베딩을 삭제하지 않는다.
- 전체 테스트 직후 실제 쿼리 결과: 개발 DB `6 documents / 24 chunks`, 테스트 DB `0 documents / 0 chunks`.
- 유료 API나 외부 Embedding 서비스는 사용하지 않았다.

## 2026-09-20 — 4단계 완료

### 현재 단계

- 0단계 환경과 뼈대: 완료
- 1단계 작업 접수와 멱등성: 완료
- 2단계 Outbox와 RabbitMQ: 완료
- 3단계 문서 적재와 검색: 완료
- 4단계 LangGraph 기본 Workflow: 완료
- 5단계 이후: 미구현(사용자 요청 범위 밖)

### 설계 경계와 요구사항 보정

- LangGraph 상태에는 직렬화 가능한 도메인 값만 저장하고 DB Session, HTTP Client, 검색·생성 Client는 노드 객체에 주입했다.
- 4단계 검증 조건과 AI 하네스 규칙을 만족하려고 이번 검색의 `(document_id, chunk_id)` 집합 밖 인용을 거부하는 최소 검사를 포함했다.
- 관련성 재판정, 검색어 재작성, 재생성 제한, 인용 영속화, `review_queue`, 승인·반려 API는 로드맵 5단계이므로 구현하지 않았다.
- PostgreSQL Checkpoint는 같은 `thread_id`의 완료된 노드를 건너뛰고 실패한 노드부터 재개한다.
- Worker 프로세스 강제 종료 후 RabbitMQ 재전달을 자동 재선점하는 정책은 `PROCESSING` Lease·오류 분류가 필요한 6단계다. 4단계에서는 새 Worker 인스턴스와 새 Checkpointer 연결이 같은 그래프 실행을 재개하는 경계를 검증했다.

### 완료 항목

- `WorkflowState`, 입력·의도·답변 Pydantic Schema와 의도·위험 Enum
- 입력 검증, 구조화 의도 분류, pgvector 검색, 구조화 답변 생성 LangGraph 노드
- 위험 질문 조기 종료와 검색 근거 없음의 `REVIEW_REQUIRED` 안전 응답
- Ollama `/api/generate` JSON Schema 요청, 비스트리밍 완료 검증, timeout·HTTP·출력 계약 오류 구분
- 검색 Chunk만 포함하는 중앙 Prompt와 허용 인용 ID 검사
- `AsyncPostgresSaver` 연결, 설정 URL 변환, strict msgpack 설정
- 같은 `thread_id` Checkpoint를 이용한 새 실행·재개·완료 결과 재사용
- `PROCESSING -> COMPLETED/REVIEW_REQUIRED` 조건부 DB 상태 전이와 결과 답변 저장
- RabbitMQ Consumer와 Workflow Handler를 연결한 상시 `worker` Compose 서비스
- 노드별 시작·완료·실패, `job_id`, `thread_id`, 수행 시간 구조 로그
- 실행·모델 준비·재개 범위·후속 단계 경계를 `docs/WORKFLOW.md`에 기록

### 자동 테스트

```text
docker compose --profile test run --build --rm test
최종 결과: 55 passed in 1.99s

docker compose --profile test run --rm --no-deps test python -m compileall -q app tests migrations
결과: 성공

docker compose --profile test run --rm --no-deps test python -m pip check
결과: No broken requirements found.

docker compose config --quiet
결과: 성공

git diff --check
결과: 성공
```

검증 범위:

- 정상: 구조화 의도·답변, 실제 PostgreSQL/pgvector 검색, 네 노드 완료, 실제 검색 Chunk ID 인용, DB 완료 상태
- 경계: 공백·최대 길이 초과 입력, 완료 Checkpoint 재사용, 검색 근거 없음의 검토 전환
- 실패: Ollama timeout, 잘못된 구조화 출력, 검색 집합 밖 인용 차단, 노드 실패 로그
- Checkpoint: 네 노드 상태가 실제 PostgreSQL 기록에 남는지 확인
- 재개: 생성 노드 실패 후 Checkpointer 연결과 그래프를 새로 만들어 같은 작업 재개, 의도 분류·검색은 반복하지 않고 생성만 재실행
- 회귀: 0~3단계 Slack 서명, 멱등 저장, Outbox, 실제 RabbitMQ, 문서 적재·검색 테스트 포함 전체 55건 통과

### 해결한 검증 이슈

- 최초 LangGraph PostgreSQL 통합 테스트 수집 시 slim 이미지에 시스템 `libpq`가 없어 Psycopg를 불러오지 못했다. 무료 오픈소스 `psycopg[binary]` 3.3.6을 직접·잠금 의존성에 고정하고 재실행했다.
- 첫 전체 실행에서 통합 테스트가 Markdown Loader에 문자열 경로를 넘겨 2건 실패했다. 계약대로 `Path`를 사용하도록 수정한 뒤 새 통합 테스트 2건과 전체 55건을 재실행해 통과했다.

### 실제 환경 확인

- 개발 DB의 실제 Nomic 적재 데이터: `6 documents / 24 chunks` 유지
- 테스트 DB 정리 상태: `0 documents / 0 chunks / 0 checkpoints`
- 실행 중 서비스: API·PostgreSQL·RabbitMQ healthy, Outbox Publisher running
- Ollama 설치 모델: `nomic-embed-text:latest` 한 개, 768차원 Embedding 기능 확인

### 남은 제한 사항

- 기본 생성 모델 `qwen3:1.7b`가 로컬 Ollama에 아직 설치되지 않아 실제 생성 모델 E2E는 실행하지 않았다. 자동 테스트는 HTTP Mock과 결정적 Fake 모델로 정상·경계·실패 계약을 검증했다.
- 답변 조회 API와 실제 Slack Thread 발신은 후속 단계 범위다. 현재 답변은 `ai_job.result_answer`에 저장한다.
- `REVIEW_REQUIRED` 상태는 남기지만 검토 Queue와 사람 승인 흐름은 5단계 범위다.
- 강제 종료 후 `PROCESSING` 작업의 RabbitMQ 자동 재선점, 오류 분류, 제한 재시도와 DLQ 정책 연결은 6단계 범위다.

### 다음 작업

사용자 확인 후에만 5단계 검증·재검색·사람 검토를 구현한다.

## 2026-09-20 — 실제 Qwen 후속 검증

### 환경

- 생성 모델: `qwen3:1.7b`
- 모델 digest: `8f68893c685c`
- 로컬 파일 크기: 1,359,293,444 bytes
- Ollama 표시 파라미터: 2.0B, `Q4_K_M`
- 기능: completion, tools, thinking
- Embedding: `nomic-embed-text:latest`, 768차원
- 실행 경로: 로컬 HTTP 접수 → Outbox → RabbitMQ → Worker → LangGraph → Ollama/pgvector → PostgreSQL
- Worker 수: 1

### 실제 실행에서 발견한 문제와 수정

- 첫 정산 절차 질문과 고객 공지 정보 질문을 Qwen이 단어만 보고 `HIGH`로 과잉 분류해 `REVIEW_REQUIRED`로 중단했다.
- 위험도 Prompt를 실제 변경·승인·삭제·발송 요구인지 판단하도록 보강하고, 읽기 전용 정보 질문과 실행 요청의 한국어 예시를 추가했다.
- Worker에 INFO 로그 초기화가 없어 자동 테스트의 `caplog`에서는 보이던 노드 로그가 실제 컨테이너 표준 출력에는 나타나지 않았다.
- `configure_logging`을 추가해 노드 시작·완료·실패와 수행 시간이 실제 Worker 로그에 출력되도록 수정했다.

### 최종 실제 E2E 결과

질문: `고객 공지에 포함하면 안 되는 정보는 무엇인가요?`

- 작업 상태: `COMPLETED`
- 시도 횟수: 1
- 실행 노드: 입력 검증, 의도 분류, 검색, 답변 생성 모두 완료
- PostgreSQL Checkpoint: 6건
- 검색 Chunk: 5건
- 생성 답변: `고객 공지에 포함하면 안 되는 정보는 '추측이나 확인되지 않은 복구 시각'이 포함되어 있다.`
- 인용 Chunk: 2건, 모두 이번 실행의 검색 Chunk ID 집합에 포함
- Worker 로그: 각 노드의 시작·완료와 `job_id`, `thread_id`, 수행 시간 출력 확인
- Queue: 처리 후 Ready/Unacked 메시지 없음

실제 실행은 구조화 출력, 검색 ID 허용목록, Checkpoint, DB 완료 상태까지 검증했다. 인용 두 건 중 일부는 답변 주장을 직접 뒷받침하는 의미적 근거가 약했다. ID가 실제 검색 결과라는 구조 검증은 통과했지만 의미적 관련성·인용 정확성은 5단계에서 별도로 평가하고 검토 전환해야 한다.

### 회귀 검증

```text
docker compose --profile test run --build --rm test
결과: 55 passed in 3.42s
```

- 실제 Qwen 검증 후에도 개발 DB의 실제 매뉴얼과 임베딩은 유지했다.
- 유료 API나 외부 생성 서비스를 사용하지 않았다.

## 2026-09-20 — 5단계 완료

### 현재 단계

- 0~4단계: 완료
- 5단계 검증·재검색·사람 검토: 완료
- 6단계 이후: 미구현(사용자 요청 범위 밖)

### 4단계 병합

- 사용자의 병합 요청에 따라 GitHub PR #3을 병합했다.
- 병합 커밋: `fe3fbde22329ebba96736ca9ed5b7d7918929c96`
- 최신 `main`에서 `feat/stage-5-validation-review` 브랜치를 생성했다.

### 설계 경계와 요구사항 보정

- Slack Thread ID와 LangGraph Checkpoint 키를 분리했다. 작업별 `{job_id}:run:{workflow_revision}`을 사용해 같은 Slack Thread의 여러 질문이 상태를 공유하지 않게 했다.
- 검토 재검색은 새 Outbox를 만들지 않고 작업별 UNIQUE Outbox를 `READY`로 되돌리며 `workflow_revision`을 한 번 증가시킨다.
- 선택 사항인 LangGraph `interrupt` 대신 PostgreSQL `review_queue`를 검토 원장으로 사용한다. 재검색은 새 Workflow 세대로 시작한다.
- 초안과 허용된 관련 인용이 없는 민감·근거 부족 항목은 승인할 수 없고, 문서 보완 후 재검색하거나 반려해야 한다.
- 작은 로컬 모델의 관련성 판정을 단독으로 신뢰하지 않는다. LLM이 선택한 Chunk를 질문 핵심어의 실제 문서 중첩으로 다시 제한하며, 불확실하면 자동 완료보다 검토를 선택한다.

### 완료 항목

- 검색 결과 관련성·문서 충돌 구조화 판정
- 검색어 재작성 기본 최대 1회와 답변 생성 기본 최대 2회 제한
- 답변 Pydantic Schema, 현재 관련 Chunk 인용 허용목록, 민감 문자열 검사
- 중복 인용의 단일 영속화와 조작 Chunk ID 차단
- `workflow_revision`, `review_queue`, `answer_citation` Migration `0004`
- 검토 대기 목록·상세 조회 API
- 승인, 수정 승인, 재검색, 반려 API와 행 잠금 기반 멱등 상태 전이
- 수정 승인 인용을 원 Workflow의 관련 Chunk 집합으로 제한
- Workflow 결과와 검토/인용을 같은 트랜잭션으로 저장
- 검토 절차와 안전 경계를 `docs/REVIEW.md`에 기록

### 자동 테스트

```text
docker compose --profile test run --build --rm test
최종 결과: 64 passed in 3.98s

docker compose --profile test run --rm --no-deps test python -m compileall -q app tests migrations
결과: 성공

docker compose --profile test run --rm --no-deps test python -m pip check
결과: No broken requirements found.

docker compose config --quiet
결과: 성공

git diff --check
결과: 성공
```

검증 범위:

- 정상: 관련 근거 답변 완료, 인용 영속화, 승인·수정 승인, 목록 조회
- 경계: 문서 없음의 재작성 정확히 1회, 같은 인용 반복의 한 건 저장, 검토 재검색 세대 한 번 증가
- 실패: 조작 Chunk ID, 원 검색 집합 밖 수정 인용, 잘못된 관련성 출력, 출력 Schema 위반
- 멱등성: 승인·재검색·반려를 두 번 호출해도 상태 변경과 인용 저장이 한 번만 적용
- 실제 인프라: PostgreSQL Checkpoint·검토·인용 트랜잭션과 RabbitMQ를 포함한 전체 회귀 64건 통과

### 실제 로컬 Ollama E2E

환경:

- 생성 모델: `qwen3:1.7b`
- Embedding 모델: `nomic-embed-text:latest`
- 개발 DB: 실제 매뉴얼 6개, Chunk 24개
- 실행 경로: 로컬 HTTP → Outbox → RabbitMQ → Worker → LangGraph → Ollama/pgvector → PostgreSQL

최종 결과:

- `정산 배치 마감 전에 무엇을 확인해야 하나요?`: `COMPLETED`, 시도 1회, `answer_citation` 1건
- `화성 기지의 산소 배급 승인 절차는 무엇인가요?`: 검색어 재작성 1회와 검색 2회 후 `REVIEW_REQUIRED`
- 검토 행: `reason_code=INSUFFICIENT_EVIDENCE`, `status=WAITING`, 허용 인용 0건
- 검토 목록 API에서 해당 행 조회 성공
- Migration 현재 버전: `0004`
- API·PostgreSQL·RabbitMQ healthy, Worker·Outbox Publisher running

실제 실행에서 처음 발견한 문제와 수정:

- Qwen이 같은 유효 Chunk를 두 번 인용해 `uq_answer_citation_job_chunk` 제약이 저장을 차단했다. 저장 전에 동일 `(document_id, chunk_id)`를 한 건으로 정규화하고 실제 PostgreSQL 회귀 테스트를 추가했다.
- Qwen이 문서 밖 화성 질문에 높은 유사도 후보를 관련 있다고 판정했다. 질문 핵심어가 실제 문서에 충분히 존재하는지 코드로 재검사해 한 번 재검색 후 검토로 전환했다.
- 최초 진단 실행의 두 작업은 `DEAD_LETTER/WORKER_INTEGRITYERROR`로 개발 DB에 그대로 남겨 오류 결과를 숨기거나 성공으로 바꾸지 않았다.

### 남은 제한 사항

- 핵심어 안전 검사는 보수적이어서 동의어만 사용하는 질문을 자동 완료하지 않고 검토로 보낼 수 있다. 평가 데이터 확대와 임계값 조정은 9단계 품질 평가에서 수행한다.
- 검토 API는 현재 로컬 개발용이며 인증·검토자 식별·감사 주체 기록은 운영 노출 전에 필요하다.
- Worker 강제 종료 후 오래된 `PROCESSING` 자동 회수, 오류 분류·지수 Backoff·DLQ 상태 동기화는 6단계 범위다.
- 실제 Slack Thread 발신과 Workspace E2E는 7단계 범위다.

### 다음 작업

사용자 확인 후에만 6단계 오류 분류·제한 재시도·자동 복구를 구현한다.

## 2026-09-23 — 5A단계 선택형 Jev 관련성 실험 완료

### 현재 단계

- 0~5단계: 완료
- 5A단계 선택형 Jev 관련성 실험: 완료
- 6단계 이후: 미구현(현재 브랜치 범위 밖)

### 설계 경계와 무료 실행 보장

- 기본 관련성 판정은 계속 로컬 Ollama이며 `WORKFLOW_DOCUMENT_GRADER=jev`를 명시한 경우에만 Jev를 선택한다.
- Jev는 pgvector 검색 후보의 Chunk별 직접 관련성과 문서 충돌 확률만 판정한다. 답변 생성, DB 쓰기, 최종 완료 여부 결정 권한은 주지 않는다.
- 외부 판정 뒤에도 기존 검색 Chunk ID 허용목록, 질문 핵심어, 인용 Schema 검증을 그대로 적용한다.
- Jev timeout, HTTP 오류, 응답 계약 위반은 기본 설정에서 로컬 Ollama 판정으로 폴백한다.
- Vercel 공식 모델 페이지에서 2026-09-25까지 무료 프로모션임을 2026-09-23 재확인했다. 그 다음 날부터 외부 호출을 차단하며 환경변수만으로 검증된 종료일을 늦출 수 없다.
- 자동 테스트는 API Key나 외부 네트워크 없이 `httpx.MockTransport`와 가상 운영 문서로만 실행한다.

### 완료 항목

- 답변 생성 Client와 분리된 `DocumentGradeClient` 경계
- Vercel 공개 Evaluation HTTP API `POST /v1/evaluate` 요청·응답 Pydantic Schema
- 여러 Chunk 관련성과 문서 충돌을 한 요청에서 판정하고 설정 임계값으로 변환
- Secret 환경변수 주입, 빈 Key 차단, 질문·문서·Secret 비로그 정책
- 외부 장애 시 로컬 Ollama 폴백과 오류 종류만 남기는 안전 로그
- 확인된 무료 종료일 이후 로컬 판정 자동 선택과 종료일 상한 검증
- Worker 자원 생성·종료와 Workflow 의존성 주입 연결
- `.env.example`, Compose, README, 아키텍처, Workflow, 요구사항 검토, 실험 가이드 갱신

### 공식 문서 대조에서 수정한 사항

- 최초 작업 트리는 SDK 내부용 `/v4/ai/evaluation-model` 규격과 전용 Header를 사용하고 있었다.
- Vercel 공식 Evaluation 문서가 공개 HTTP 경계로 `/v1/evaluate`와 본문의 `model`, `state`, `questions`를 명시하는 것을 확인했다.
- 내부 구현 복제 대신 공개 API 계약으로 교체하고 URL, 본문, Authorization Header를 Mock 계약 테스트로 고정했다.

### 자동 테스트

```text
docker compose --profile test run --build --rm test pytest -q tests/unit/test_jev_document_grader.py tests/unit/test_worker_main.py tests/unit/test_workflow_graph.py
결과: 15 passed in 0.54s

docker compose --profile test run --rm test
결과: 74 passed in 3.45s

docker compose --profile test run --rm --no-deps test python -m compileall -q app tests migrations
결과: 성공

docker compose --profile test run --rm --no-deps test python -m pip check
결과: No broken requirements found.

docker compose config --quiet
결과: 성공

git diff --check
결과: 성공
```

검증 범위:

- 정상: 공개 HTTP 요청 계약, 확률 임계값 변환, Workflow의 별도 판정 Client 사용
- 경계: 무료 종료일 당일까지 Jev 선택, 다음 날 로컬 선택, 환경변수 종료일 연장 거부
- 실패: 빈 API Key, 응답 누락·Schema 위반, timeout, Jev 오류 뒤 로컬 폴백
- 보안: 성공·폴백 로그에 질문, Chunk 본문, Secret을 남기지 않음
- 회귀: PostgreSQL, pgvector, RabbitMQ, Slack 수신, LangGraph Checkpoint, 검토 API를 포함한 기존 0~5단계 전체 테스트

### 남은 제한 사항

- 실제 Jev API Key를 저장소나 로그에 주입하지 않았으므로 실제 외부 호출은 실행하지 않았다. 공개 HTTP 계약은 공식 문서 대조와 Mock Transport로 검증했다.
- Jev 무료 프로모션은 2026-09-25 종료 예정이므로 장기 기본 경로로 사용할 수 없다. 기본 Ollama Workflow는 영향을 받지 않는다.
- Jev와 Ollama의 대표 질문별 정확도 비교는 고정 평가 데이터셋을 만드는 9단계에서 측정해야 한다. 측정 전에는 Jev 확률을 품질 개선 근거로 단정하지 않는다.

### 다음 작업

사용자 확인 후에만 6단계 오류 분류·제한 재시도·자동 복구를 구현한다.
