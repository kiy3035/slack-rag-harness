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

## 2026-09-23 — 6단계 장애 복구 완료

### 현재 단계

- 0~5단계: 완료
- 5A단계 선택형 Jev 관련성 실험: 완료
- 6단계 오류 분류·제한 재시도·자동 복구: 완료
- 7단계 이후: 미구현(현재 브랜치 범위 밖)

### 5A단계 병합

- GitHub PR #5 병합 결과를 `origin/main`에서 가져왔다.
- 병합 커밋: `2ee5f77`
- 최신 `main`에서 `codex/stage-6-recovery` 브랜치를 생성했다.

### 설계 경계

- Worker 예외 원문을 저장하지 않고 타입 기반 오류 코드와 짧은 안전 문구만 DB·로그에 남긴다.
- Ollama timeout·HTTP 실패와 PostgreSQL 연결 오류는 일시 오류, 생성 모델 구조화 출력 위반은 사람 검토 필요, 입력·임베딩 계약 위반과 예상하지 못한 오류는 영구 오류로 분리한다.
- 일시 오류는 `PROCESSING → RETRY_WAIT`으로 전이하고 작업 실행 횟수 기반 지수 Backoff를 적용한다. 시각이 지나면 고유 Outbox를 `READY`로 다시 열어 같은 Workflow 세대 Checkpoint에서 재개한다.
- Worker 강제 종료는 `locked_at` Lease 만료로 감지한다. 기본 Lease 900초는 기본 Ollama 생성 timeout보다 길게 두고 테스트에서만 짧게 덮어쓴다.
- 재시도 소진과 영구 오류는 `DEAD_LETTER` 상태를 먼저 저장한다. DLQ 발행은 별도 Lease, 실패 횟수, 다음 재시도 시각, Publisher Confirm 완료 시각으로 복구한다.
- DLQ Confirm 성공 직후 DB 기록 전에 종료되는 극단적인 구간에는 동일 `message_id`가 중복될 수 있으므로 DLQ 소비자는 멱등해야 한다.
- 관리자 수동 재처리는 기본 비활성화다. `FAILED/DEAD_LETTER`만 허용하고 작업별 `idempotency_key`를 `job_recovery_request`에 저장해 Workflow 세대를 한 번만 올린다.
- 수동 재처리 Endpoint는 로컬 장애 조치용이며 외부 공개 전 인증·권한 계층이 필요하다.

### 완료 항목

- `FailureKind`, `FailureDecision`, `ErrorClassifier`, `RetryPolicy`
- Worker Consumer의 일시 오류 Backoff, 출력 오류 검토 전환, 영구·소진 오류 DLQ 전환
- `RETRY_WAIT` 만기 작업의 기존 Outbox 원자적 재개
- 오래된 `PROCESSING` 작업의 Lease 복구와 최대 횟수 제한
- DB 원장 기반 미발행 DLQ 선점·발행·실패 Backoff·Publisher Confirm 기록
- 모델 출력 계약 오류의 빈 근거 `review_queue` 생성과 무한 재시도 차단
- `POST /api/v1/admin/jobs/{job_id}/retry` 멱등 수동 재처리 Endpoint
- DLQ 상태 열, 복구 조회 인덱스, `job_recovery_request` Migration `0005`
- Worker에 Recovery Scheduler 연결과 모든 복구 임계값 환경변수 분리
- `docs/RECOVERY.md`, README, 아키텍처, Workflow 문서 갱신

### 자동 테스트

```text
docker compose --profile test run --build --rm test
결과: 81 passed in 4.80s

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

- 일시 오류: 실제 RabbitMQ Consumer의 Fake Ollama timeout을 `RETRY_WAIT`으로 저장한 뒤 ACK하고 Queue에 중복 메시지가 남지 않음
- Backoff: 첫 실패 5초 전에는 재개하지 않고 만기 시 기존 Outbox를 `READY`로 다시 엶
- 검토 필요: 잘못된 모델 JSON을 `MODEL_OUTPUT_INVALID` 검토 항목 한 건으로 전환
- Worker 종료: 오래된 `PROCESSING`의 제한 복구와 횟수 소진 시 `DEAD_LETTER` 전환
- DLQ 연결: 실제 RabbitMQ DLQ Header와 DB `dlq_published_at`을 함께 확인
- 수동 복구: 같은 관리자 멱등 키를 두 번 보내도 Workflow 세대가 한 번만 증가하고 복구 원장 한 건만 생성
- DB 오류: SQLAlchemy 연결 오류를 일시 오류로 분류하고 ACK 전 DB 상태 저장을 강제
- 중복 메시지: 기존 실제 RabbitMQ 통합 테스트가 Handler 한 번 실행을 계속 보장
- 회귀: Slack 서명·접수, Outbox, RabbitMQ, 검색, LangGraph Checkpoint, 검토 API, Jev 선택형 실험 포함 전체 테스트 통과

### Migration과 실제 서비스 검증

```text
alembic downgrade 0004
결과: 0005 → 0004 성공

alembic upgrade head
결과: 0004 → 0005 성공

docker compose up --build -d api outbox-publisher worker
결과: api healthy, postgres/rabbitmq healthy, outbox-publisher/worker running

SELECT version_num FROM alembic_version
결과: 0005
```

- 개발 DB에 이전 단계에서 보존된 `DEAD_LETTER/WORKER_INTEGRITYERROR` 작업 2건이 있었다.
- 6단계 Worker 시작 시 두 건을 미발행 DLQ 대상으로 선점해 실제 RabbitMQ DLQ에 발행하고 `dlq_published_at`을 기록했다.
- `ENABLE_ADMIN_RECOVERY=false` 상태에서 실제 관리자 재처리 요청이 `404 NOT_FOUND`를 반환하는 것을 확인했다.

### 남은 제한 사항

- PostgreSQL 컨테이너를 실제 중단하는 파괴적 장애 주입은 전체 테스트 실행 중 다른 검증을 방해하므로 자동 테스트에서는 SQLAlchemy 연결 오류 분류와 ACK 전 상태 저장 경계로 검증했다.
- RabbitMQ가 Outbox 최대 발행 횟수 동안 복구되지 않으면 기존 2단계 정책대로 작업은 `FAILED`에 남고 관리자 수동 재처리가 필요하다.
- DLQ는 최소 한 번 발행 계약이므로 Confirm 성공과 DB 기록 사이의 종료 시 중복 가능성이 남는다.
- 관리자 수동 재처리 API는 로컬 전용이며 인증·권한·관리자 신원 감사는 운영 공개 전에 추가해야 한다.
- 실제 Slack Thread 발신과 무료 Workspace 화면 E2E는 7단계 범위다.

### 다음 작업

사용자 확인 후에만 7단계 실제 Slack 연동과 Thread 답변 발신을 구현한다.

## 2026-09-29 — 7단계 Slack 연동 구현 완료, 실제 Workspace E2E 대기

### 현재 단계

- 0~6단계: 완료
- 5A단계 선택형 Jev 관련성 실험: 완료
- 7단계 실제 Slack 연동 코드·자동 검증: 완료
- 7단계 무료 Slack Workspace 화면 E2E: 사용자 로컬 설정 대기
- 8단계 이후: 미구현(현재 브랜치 범위 밖)

### 6단계 병합

- GitHub PR #6이 `main`에 병합된 것을 확인했다.
- 병합 커밋: `f253662`
- 6단계 구현 커밋 `335afff`에서 `codex/stage-7-slack-integration` 브랜치를 생성했다.

### 설계 경계

- Workflow 자동 완료와 사람 검토 승인은 최종 답변 저장 트랜잭션 안에서 `slack_reply_outbox`를 함께 생성한다.
- 작업별 Slack Outbox는 `UNIQUE(job_id)`로 중복 예약을 막는다.
- Slack API 호출은 DB 트랜잭션 밖에서 수행하고 `FOR UPDATE SKIP LOCKED`와 Lease로 다중 Worker 선점을 제어한다.
- Slack HTTP 429는 `Retry-After`와 지수 Backoff 중 큰 값을 사용해 원격 제한 시각 전에 재호출하지 않는다.
- Timeout·네트워크·HTTP 5xx·Slack 내부 오류만 제한 재시도하고, 채널·권한 등 영구 오류는 즉시 `FAIL`로 전환한다.
- 원격 오류 본문, 질문, 답변, Bot Token은 로그에 남기지 않는다.
- 원본 `channel`과 `event.thread_ts` 또는 `event.ts`를 유지해 같은 Thread에 답변한다.
- 발신 `reply_id`를 `client_msg_id`로 전달한다. Slack 성공 직후 DB 기록 전 종료되는 극단적인 구간의 중복 가능성은 남는다.
- 실제 Slack 발신은 `SLACK_REPLY_ENABLED=true`일 때만 활성화한다. 기본 로컬·CI 경로는 Slack 연결 없이 동작한다.

### 완료 항목

- 선두 Bot 멘션 제거와 빈 질문·Bot·subtype 이벤트 무시
- 기존 Thread에서는 부모 `thread_ts`, 최상위 멘션에서는 `event.ts` 보존
- Bot Token Authorization Header 기반 `chat.postMessage` Client
- 링크·미디어 자동 미리보기 비활성화
- 성공 응답 `ts`, API 오류, HTTP 429 `Retry-After`, timeout·5xx 분류
- `slack_reply_outbox` 모델과 Migration `0006`
- 자동 완료·검토 승인 답변의 트랜잭션 Outbox 예약
- Slack 답변 Outbox 선점, Lease 회수, 지수 Backoff, 최대 시도, 완료 Timestamp 기록
- Worker loop에 선택형 Slack Reply Publisher 연결
- 환경변수와 Compose 설정 추가
- `docs/SLACK_SETUP.md`, README, 프로젝트 개요, 아키텍처 문서 갱신

### 자동 테스트

```text
docker compose --profile test run --build --rm test
결과: 90 passed in 5.02s

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

- 수신 정상: `app_mention`의 Bot 멘션 제거, 원본 채널·Thread 보관, 3초 이내 ACK
- 수신 경계: 기존 Thread 부모 유지, 멘션만 있는 빈 질문 무시, 모든 subtype 무시
- 수신 중복: 동일 `event_id`와 Slack 재전송을 기존 DB UNIQUE 경계로 한 작업에 수렴
- 발신 정상: 완료 답변을 원본 Thread로 전송하고 Slack 응답 `ts`와 `sent_at` 저장
- 검토 승인: 사람 승인 재호출에도 Slack Outbox 한 건만 생성
- Rate Limit: 429 `Retry-After` 전 재선점 차단과 지정 시각 재시도
- 실패: 일시 오류 최대 횟수 소진 뒤 `FAIL`, 영구 API 오류 즉시 중단
- 보안: Token은 Authorization Header로만 전송하고 오류 원문·질문·답변을 로그에서 제외
- 회귀: PostgreSQL, pgvector, RabbitMQ, LangGraph Checkpoint, 검토, 장애 복구, Jev 선택형 실험 포함 전체 테스트 통과

### Migration과 실제 서비스 검증

```text
alembic downgrade 0005
결과: 0006 → 0005 성공

alembic upgrade head
결과: 0005 → 0006 성공

docker compose up --build -d api outbox-publisher worker
결과: api healthy, postgres/rabbitmq healthy, outbox-publisher/worker running

GET http://localhost:8000/health/ready
결과: {"status":"ok"}

SELECT version_num FROM alembic_version
결과: 0006
```

- `.env` 파일은 존재하지만 `SLACK_REPLY_ENABLED=false`이고 Bot Token·Signing Secret은 비어 있음을 값 노출 없이 확인했다.
- 비활성 설정에서 Worker가 Slack API를 호출하지 않고 정상 실행되는 것을 확인했다.

### 남은 제한 사항

- 무료 Slack Workspace, App, Bot Token, Signing Secret이 아직 설정되지 않아 Slack PC 앱의 실제 질문·Thread 답변 화면 E2E는 실행하지 못했다.
- TryCloudflare Quick Tunnel은 개발·시연 전용이며 주소와 가용성이 보장되지 않는다.
- Slack 성공 직후 DB 완료 기록 전 Worker 종료 시 같은 `client_msg_id`의 재호출 가능성이 남는다.
- `FAIL` 답변을 운영자가 다시 여는 관리자 기능은 아직 없으며 DB 상태와 안전한 오류 코드로 진단해야 한다.

### 다음 작업

사용자가 `docs/SLACK_SETUP.md`에 따라 무료 Workspace와 로컬 Secret을 설정하면 실제 Slack PC 앱 E2E를 완료한다. 이후 8단계 관측 화면을 구현한다.

## 2026-09-29 — 8단계 관측 화면 구현 완료

### 현재 단계

- 0~8단계: 구현 완료
- 7단계 무료 Slack Workspace 화면 E2E: 사용자 로컬 설정 대기
- 9단계 이후: 미구현(현재 브랜치 범위 밖)

### 7단계 병합

- GitHub PR #7이 `main`에 병합된 것을 확인했다.
- 병합 커밋 `aff91ec`에서 `codex/stage-8-observability` 브랜치를 생성했다.

### 설계 경계

- PostgreSQL의 작업·검토 상태를 원장으로 유지하고 Prometheus Scrape 시점에 상태별 Gauge를 집계한다.
- API 요청은 실제 Route Template을 Label로 사용해 동적 ID에 따른 고카디널리티를 피한다.
- API, Worker, Outbox Publisher는 각각 독립 메트릭 Endpoint를 제공한다.
- Workflow 전체·노드·Ollama 호출시간은 Histogram으로 기록하며 임의의 성능 수치를 문서에 기입하지 않는다.
- JSON 로그 Formatter는 허용된 상관관계·상태·오류 필드만 기록하고 질문, 답변, 문서 본문, Token과 예외 원문을 제외한다.
- 최소 관리 화면은 식별자·상태·시도 횟수·안전한 사유 코드와 시각만 보여 주며 질문·답변·검토 의견은 노출하지 않는다.
- 관리 화면은 애플리케이션 기본값에서 비활성화하고 로컬 Compose만 활성화한다.
- Grafana·Prometheus·Loki·RabbitMQ 메트릭 포트는 `127.0.0.1`에만 바인딩한다.
- Grafana의 원격 사용 통계, 업데이트 확인, 기본 Plugin 설치와 자동 업데이트를 비활성화한다.

### 완료 항목

- `prometheus-client` 기반 API 요청량·지연, 작업·검토 상태, Workflow·노드·Ollama 지연, Outbox·복구·DLQ 메트릭
- RabbitMQ `rabbitmq_prometheus` Plugin과 Queue 메트릭 Endpoint
- Prometheus 7일 로컬 보관과 API·Worker·Outbox·RabbitMQ Target 구성
- 10개 패널의 `Slack RAG Harness 관측` Grafana Dashboard 자동 Provisioning
- Alloy의 공유 JSON 로그 Tail과 Loki 전달
- 최근 작업과 검토 대기 상태를 표시하는 `/admin` 최소 관리 화면
- 관리 화면·메트릭·로그 보안 경계를 검증하는 단위·통합 테스트
- `docs/OBSERVABILITY.md`, README, 프로젝트 개요, 아키텍처 문서 갱신

### 자동 테스트

```text
docker compose --profile test run --build --rm test
첫 실행 결과: 기존 RabbitMQ Retry Queue 도착 2초 경계에서 1건 일시 실패, 95 passed

docker compose --profile test run --rm test pytest -q tests/integration/test_stage2_messaging.py::test_retry_and_dead_letter_queues_route_messages
결과: 1 passed in 0.45s

docker compose --profile test run --rm test
최종 결과: 96 passed in 6.26s

docker compose --profile test run --rm --no-deps test python -m compileall -q app tests migrations
결과: 성공

docker compose --profile test run --rm --no-deps test python -m pip check
결과: No broken requirements found.

docker compose config --quiet
결과: 성공

git diff --check
결과: 성공
```

첫 전체 실행의 실패는 같은 Image와 Broker에서 해당 테스트를 즉시 단독 실행하고 전체 Suite를 다시 실행했을 때 재현되지 않았다. 검증 기준이나 Timeout은 변경하지 않았다.

### 실제 서비스 검증

```text
docker compose up --build -d
결과: API healthy, PostgreSQL/RabbitMQ healthy, Worker/Outbox/Prometheus/Grafana/Loki/Alloy 실행 중

GET /health/ready
결과: ok

GET /admin
결과: HTTP 200

GET /metrics
결과: API와 DB 집계 메트릭 노출

Prometheus /api/v1/targets
결과: rabbitmq, slack-rag-api, slack-rag-worker, slack-rag-outbox 모두 up

RabbitMQ /metrics
결과: rabbitmq_queue_messages_ready 노출

Loki /ready 및 API 로그 Query
결과: ready, api Stream 1개 이상 확인

Grafana /api/health 및 Dashboard API
결과: database ok, `Slack RAG Harness 관측` 10개 패널 확인
```

### 무료·로컬 구성

- `prometheus-client==0.26.0`: Apache-2.0/BSD-2-Clause
- `prom/prometheus:v3.14.0`: Apache-2.0
- `grafana/grafana:13.1.6`: AGPL-3.0
- `grafana/loki:3.7.7`: AGPL-3.0
- `grafana/alloy:v1.20.0`: Apache-2.0
- 모든 구성은 로컬 Docker Compose에서 실행하며 유료 API와 관리형 관측 서비스를 사용하지 않는다.

### 남은 제한 사항

- `/admin`은 최소 로컬 화면이며 사용자 인증·권한·감사 기능이 없다. 외부 공개 전에는 비활성 상태를 유지해야 한다.
- Grafana 기본 계정은 로컬 예시이므로 외부 접근 환경에서는 비밀번호 변경과 별도 접근 통제가 필요하다.
- Loki는 단일 로컬 인스턴스이며 고가용성·원격 백업을 제공하지 않는다.
- 실제 Slack PC 앱 화면 E2E는 7단계에서와 같이 사용자 Workspace Secret 설정을 기다린다.
- 성능 수치는 아직 측정하지 않았으며 9단계 평가와 10단계 부하 테스트에서 실행 환경과 함께 기록한다.

### 다음 작업

8단계 PR 병합 후 9단계 평가 하네스를 별도 브랜치와 PR로 구현한다.

## 2026-09-30 — 9단계 평가 하네스 구현 완료

### 현재 단계

- 0~9단계: 구현 완료
- 7단계 무료 Slack Workspace 화면 E2E: 사용자 로컬 설정 대기
- 9단계 실제 60건 기준·비교 측정: 로컬 CPU 실행시간 문제로 미완료
- 10단계: 미구현(현재 브랜치 범위 밖)

### 8단계 병합

- GitHub PR #8이 `main`에 병합된 것을 확인했다.
- 병합 커밋 `bf1dad1`에서 `codex/stage-9-evaluation-harness` 브랜치를 생성했다.

### 설계 경계

- 평가 데이터셋은 JSONL 60건으로 고정하고 답변 가능, 문서 없음, 표현 변형, 다중 문서, 문서 충돌, 민감 작업을 각각 10건씩 포함한다.
- 데이터셋은 Pydantic으로 검증하며 최소 건수, 필수 유형, 중복 ID, 유형별 기대 문서·검토 계약과 실제 문서 경로를 실행 전에 확인한다.
- 평가 실행은 운영과 같은 pgvector 검색, Workflow 노드, Ollama 생성·임베딩 모델을 사용하되 인메모리 Checkpoint를 사용하고 운영 작업·검토·RabbitMQ에는 쓰지 않는다.
- 관련성 판정은 선택형 외부 Jev 설정과 무관하게 로컬 Ollama 모델을 사용한다.
- Retrieval Recall@K는 기대 문서가 있는 Case의 Macro 평균, 인용 정확도는 실제 인용 Chunk의 Micro 평균으로 계산한다.
- 검토 전환 정확도와 결정적인 핵심어 겹침 기반 근거 없는 문장률을 함께 기록한다.
- 성능 수치는 실제 실행에서만 계산하고 플랫폼, CPU 수, 모델명, 데이터셋 해시와 모든 검색·동시성 조건을 결과에 포함한다.
- 비교 리포트는 같은 데이터셋에서 기준 실행 대비 정확히 한 조건만 바뀐 후보만 허용한다.

### 완료 항목

- 60건 고정 평가 데이터셋과 여섯 유형별 10건 균형 구성
- 데이터셋 Schema·SHA-256·문서 경로·유형별 계약 검증
- 실제 pgvector/Ollama Workflow를 재사용하는 읽기 전용 평가 실행기
- Retrieval Recall@K, 인용 정확도, 검토 전환 정확도, 근거 없는 문장률 계산
- 처리량, nearest-rank p95, 실패 Case와 안전한 오류 코드 기록
- JSON 원본과 한국어 Markdown 리포트의 원자적 저장
- Top-K, 유사도 임계값, 검색어 재작성, Worker 수의 단일 조건 비교 검증
- `docs/EVALUATION.md`, README, 프로젝트 개요, 아키텍처 문서 갱신

### 자동 테스트와 정적 검증

```text
로컬 가상환경: python -m pytest -q -p no:cacheprovider tests/unit
결과: 57 passed in 2.53s

로컬 가상환경: python -m pytest -q -p no:cacheprovider tests/contract
결과: 9 passed in 0.57s

docker compose --profile test run --build --rm test
결과: 101 passed in 6.45s

python -m compileall -q app tests migrations
결과: 성공

python -m pip check
결과: No broken requirements found.

docker compose config --quiet
결과: 성공

git diff --check
결과: 성공
```

### 데이터셋 검증

```text
python -m app.evaluation.main validate
결과: 60건, 여섯 유형 각각 10건
SHA-256: 6933a55c61df3387079afc1e4b7d3c512ef11e62d6809ed275aef55d1a1c1797
```

### 실제 측정 시도

```text
docker compose run --rm api python -m app.evaluation.main run --top-k 5 --min-score -1.0 --max-query-rewrites 1 --worker-count 1
결과: qwen3:1.7b와 nomic-embed-text를 100% CPU로 사용해 60분간 실행했으나 60건을 완주하지 못해 중단
```

완주 전에는 리포트를 원자적으로 생성하지 않으므로 측정 수치나 결과 파일은 남기지 않았다. 임의의 일부 데이터, 추정값 또는 보정값으로 대체하지 않았다. 따라서 Top-K, 유사도 임계값, 재작성, Worker 수 비교 수치도 아직 확정하지 않았다.

### 무료·로컬 구성

- 평가 실행은 기존 `qwen3:1.7b`, `nomic-embed-text`, PostgreSQL·pgvector만 사용한다.
- 외부 평가 SaaS, 유료 API와 관리형 저장소는 추가하지 않았다.
- 평가 질문·검색 본문·답변이 포함된 `evaluation/results`는 Git에서 제외한다.

### 남은 제한 사항

- CPU 전용 Worker 1개 기준 60건 실행이 60분 안에 완주하지 않아 실제 품질·처리량·p95와 조건별 비교 수치는 아직 없다.
- 현재 리포트는 전체 Case 완주 뒤 생성되므로 장시간 실행 중단 시 부분 결과를 재개하지 못한다. 장시간 반복 측정 전에 Case 단위 Checkpoint·재개 기능을 추가하는 것이 필요하다.
- 문서 충돌 Case는 현재 문서 자체의 실제 충돌이 아니라 사용자가 상충 지침을 주장할 때 안전한 검토 전환을 기대하는 계약이다.
- 실제 Slack PC 앱 화면 E2E는 7단계에서와 같이 사용자 Workspace Secret 설정을 기다린다.

### 다음 작업

9단계 PR 병합 후 장시간 평가의 Case 단위 Checkpoint·재개를 보강하고 실제 기준·단일 조건 비교 측정을 완료한다. 이후 10단계 부하 테스트와 블로그 자료를 별도 브랜치와 PR로 구현한다.

## 2026-10-05 — 9단계 장시간 평가 재개·중복 실행 방지 보강

### 현재 단계

- 9단계 평가 하네스 PR #9 병합 완료(`abb6b15`)
- Case 단위 Checkpoint·재개와 실행 ID별 프로세스 잠금 구현 완료
- Worker 1·2 실제 60건 실행과 Worker 수 단일 조건 비교 완료
- Top-K, 최소 검색 점수, 재작성 횟수 비교는 후속 장시간 측정 필요

### 완료 항목

- 실행 ID별 JSON Checkpoint에 데이터셋 해시, 평가 설정, 실행 환경, 누적 활성 실행시간과 완료 Case를 원자적으로 저장
- 같은 데이터셋·설정·환경·모델일 때만 완료 Case 이후부터 재개
- Worker 병렬 완료 순서와 무관하게 최종 결과를 원본 데이터셋 순서로 정렬
- Case 완료마다 한 줄 JSON 진행 로그 출력
- 동일 실행 ID를 운영체제 파일 잠금으로 한 프로세스만 소유하도록 제한
- Windows와 Linux 컨테이너에서 프로세스 종료 뒤 잠금 재획득 검증
- 완료된 실행 ID 재사용과 경로 이탈 가능한 실행 ID 거부

### 자동 테스트와 정적 검증

```text
로컬 가상환경: python -m pytest -q -p no:cacheprovider tests/unit
결과: 60 passed in 3.51s

docker compose --profile test run --build --rm test
결과: 104 passed in 11.49s

python -m compileall -q app tests migrations
결과: 성공

python -m pip check
결과: No broken requirements found.

docker compose config --quiet
결과: 성공

git diff --check
결과: 성공
```

로컬 제한 실행에서는 pytest 임시 디렉터리 ACL 때문에 3개 Fixture가 준비되지 않았으나, 같은 명령을 호스트 권한으로 다시 실행해 60개 전체 통과를 확인했다. Docker 전체 회귀 테스트도 별도로 통과했다.

### 실제 측정 상태

동일한 데이터셋과 모델에서 Worker 수만 1개와 2개로 바꿔 각각 60건을 완료했다. 측정 환경은 WSL2 Linux x86_64, 논리 CPU 16개, `qwen3:1.7b`, `nomic-embed-text`다.

| 실행 | Worker | Recall@5 | 인용 정확도 | 검토 전환 | 근거 없는 문장률 | 처리량(cases/s) | p95 | 총 실행시간 | 실패 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline-cpu-v3` | 1 | 74.00% | 93.62% | 70.00% | 60.12% | 0.019200 | 91,920.22ms | 52.08분 | 0 |
| `workers-2-cpu-v1` | 2 | 74.00% | 94.00% | 65.00% | 61.05% | 0.018232 | 175,151.57ms | 54.85분 | 0 |

Worker 2는 Worker 1보다 처리량이 약 5.0% 낮고 p95가 약 90.6% 높았다. 이 환경은 생성 요청이 하나의 로컬 Ollama CPU 모델을 공유하므로 Worker를 늘려도 모델 추론이 병렬 확장되지 않고 CPU·메모리 자원 경합이 커진 것으로 해석한다. 이는 측정에 근거한 환경별 결론이며 GPU나 여러 Ollama 인스턴스의 결과로 일반화하지 않는다.

품질 지표 차이는 같은 데이터셋에서도 생성 결과가 완전히 결정적이지 않으므로 Worker 수의 직접 효과로 단정하지 않는다. 두 실행 모두 실패 Case는 없었고 Retrieval Recall@5는 74%로 같았다.

최초 Worker 1 실행은 Docker CLI 중단 뒤 일회성 컨테이너가 계속 수행되어 재개 프로세스와 같은 실행 ID를 덮어썼다. 이 문제를 재현해 실행 ID별 운영체제 잠금을 추가했고, 서로 다른 컨테이너에서 두 번째 획득이 거부되는 것을 확인했다.

새 Worker 1 측정 중에도 `Ctrl+C`가 컨테이너가 아닌 Docker CLI만 종료하는 동작을 확인했다. 이후 로컬·Docker 테스트와 CPU가 겹쳤으므로 해당 부분 측정은 성능 기준에서 제외하고 실제 컨테이너 ID를 확인해 종료했다. 추정값이나 오염된 결과를 비교 수치로 사용하지 않는다.

### 남은 제한 사항

- Top-K, 최소 검색 점수, 재작성 횟수 비교는 각각 별도의 실제 60건 실행이 필요하다.
- `qwen3:1.7b`의 생성 결과가 완전히 결정적이지 않아 검색 설정 외 품질 지표의 작은 차이는 반복 실행으로 분산을 확인해야 한다.
- 이번 성능 결론은 WSL2의 단일 로컬 Ollama CPU 실행에 한정한다.
- 실제 Slack PC 앱 화면 E2E는 사용자 Workspace Secret 설정을 기다린다.

## 2026-10-09 — 9단계 검색 파라미터 실제 비교 완료

### 현재 단계

- 9단계 실제 비교 실험 완료
- 10단계 부하 테스트와 블로그 자료: 다음 단계
- 실제 Slack PC 앱 화면 E2E: 사용자 Workspace Secret 설정 대기

### 측정 환경과 기준

- 데이터셋: 고정 JSONL 60건, SHA-256 `6933a55c61df3387079afc1e4b7d3c512ef11e62d6809ed275aef55d1a1c1797`
- 환경: WSL2 Linux x86_64, 논리 CPU 16개
- 모델: `qwen3:1.7b`, `nomic-embed-text`
- 기준: Top-K 5, 최소 점수 -1.0, 재작성 1회, Worker 1개
- 각 후보는 기준에서 한 조건만 변경

### 실제 결과

| 실행 | 변경 조건 | Recall@K | 인용 정확도 | 검토 전환 | 근거 없는 문장률 | 처리량(cases/s) | p95 | 실패 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline-cpu-v3` | 기준 | 74.00% | 93.62% | 70.00% | 60.12% | 0.019200 | 91,920.22ms | 0 |
| `top-k-3-cpu-v1` | Top-K 3 | 67.00% | 92.86% | 70.00% | 59.88% | 0.019956 | 79,493.51ms | 1 |
| `top-k-8-cpu-v1` | Top-K 8 | 81.00% | 96.23% | 71.67% | 56.59% | 0.015627 | 110,563.45ms | 1 |
| `min-score-0p2-cpu-v2` | 최소 점수 0.2 | 74.00% | 93.62% | 70.00% | 60.12% | 0.020174 | 84,590.63ms | 0 |
| `no-rewrite-cpu-v1` | 재작성 0회 | 70.00% | 95.45% | 66.67% | 59.35% | 0.019936 | 91,004.60ms | 1 |

### 결론

- Top-K 3은 기준보다 빠르지만 Recall이 7%p 낮았다.
- Top-K 8은 Recall이 7%p 높고 인용 지표도 개선됐지만 처리량이 약 18.6% 낮고 p95가 약 20.3% 높았으며 Timeout 1건이 있었다.
- 최소 점수 0.2는 60건 모두에서 기준과 같은 Chunk를 검색했다. 속도 차이는 임계값 효과가 아니라 단일 실행 변동으로 본다.
- 재작성을 끄면 성능 차이는 작지만 Recall이 4%p, 검토 전환 정확도가 3.33%p 낮아졌다.
- 현재 기본값은 Top-K 5, 최소 점수 -1.0, 재작성 1회, Worker 1개로 유지한다.

### 측정 중 장애와 처리

- `min-score-0p2-cpu-v1`은 Ollama 장애로 32건이 연속 실패해 성능 비교에서 폐기했다.
- Ollama 정상 상태에서 `min-score-0p2-cpu-v2`를 새로 60건 실행해 실패 0건을 확인했다.
- `no-rewrite-cpu-v1`은 59건 저장 뒤 Windows 파일 권한 오류로 최종 Checkpoint 교체가 중단됐으나 같은 실행 ID로 마지막 1건만 재개했다.
- 오염된 실행시간과 실패로 짧아진 실행시간을 성능 개선으로 사용하지 않았다.

### 자동 검증

```text
Top-K 비교 리포트 생성: 성공
최소 검색 점수 비교 리포트 생성: 성공
검색어 재작성 비교 리포트 생성: 성공
각 후보의 데이터셋 SHA-256 일치와 단일 조건 변경 검증: 성공

docker compose --profile test run --build --rm -e WORKFLOW_DOCUMENT_GRADER=ollama test
결과: 104 passed in 5.39s

python -m compileall -q app tests migrations
결과: 성공

python -m pip check
결과: No broken requirements found.

docker compose config --quiet
결과: 성공

git diff --check
결과: 성공
```

첫 Docker 테스트는 로컬 `.env`의 폐기된 `WORKFLOW_DOCUMENT_GRADER=cloudflare_jev` 값 때문에 Migration 전에 설정 검증이 실패했다. 사용자 환경 파일을 변경하지 않고 테스트 프로세스에만 `ollama`를 주입해 전체 회귀 테스트 통과를 확인했다.

### 남은 제한 사항

- Top-K 3·8과 재작성 0회 실행에는 각각 실패 1건이 있어 작은 품질 차이는 확정적인 인과로 해석하지 않는다.
- 최소 점수 0.2가 검색 집합을 바꾸지 않아 임계값 선택을 개선하려면 경계 점수와 어려운 음성 예제가 필요하다.
- 모든 성능 결론은 WSL2 단일 로컬 Ollama CPU 환경에 한정한다.
- 실제 Slack PC 앱 화면 E2E는 사용자 Workspace Secret 설정을 기다린다.

### 다음 작업

9단계 결과 PR 병합 후 10단계 k6 부하 테스트, Queue 적체·복구 측정, Grafana 캡처 목록과 블로그 초안 자료를 별도 브랜치와 PR로 구현한다.

## 2026-10-09 — 10단계 부하 테스트와 블로그 자료 구현 완료

### 현재 단계

- 10단계 자동화·로컬 측정 완료
- 실제 Slack PC 앱 정상 답변·검토 전환 화면: 사용자 무료 Workspace 설정 대기
- 브랜치: `codex/stage-10-load-test`

### 구현과 문서

- `grafana/k6:1.8.1` 고정 이미지와 `loadtest` Compose Profile
- 실제 Slack v0 HMAC 서명을 사용하는 `app_mention` 순간 요청 시나리오
- 같은 `event_id` 중복 폭주와 고유 이벤트 모드 분리
- 실패율, Check 성공률, ACK p95 3초와 3초 이내 ACK 비율 임계값
- RabbitMQ 관리 API 응답을 Pydantic으로 검증해 Queue 변화를 JSONL로 기록하는 관측기
- 적체를 실제로 본 뒤 Queue 합계가 0일 때만 `drained=true`로 종료하는 계약
- 전체 구조도와 정상·재검색·사람 검토·장애 복구 시퀀스
- Worker 수 비교 해석, Grafana·Slack 화면 캡처 목록, 한계와 개선 방향
- `docs/LOAD_TEST.md`, `docs/BLOG_DRAFT.md`, README 갱신

k6는 AGPL-3.0 무료 오픈소스 배포판을 로컬 Docker에서만 사용한다. Grafana Cloud, 유료 API와 관리형 부하 테스트 서비스는 사용하지 않으며 사용량 보고도 비활성화했다. 합성 채널·질문만 사용했고 Slack 실제 발신은 비활성화했다.

### 실제 Webhook 측정

환경은 WSL2 Linux x86_64, 논리 CPU 16개, Docker Compose, k6 1.8.1, Worker 1개, `qwen3:1.7b`, `nomic-embed-text`다. 모델이 준비된 Warm Run이며 첫 이미지 내려받기·API 재시작 스모크 값은 성능표에서 제외했다.

| 실행 | 모드 | 요청/VU | 생성/중복 | 처리량 | ACK p95 | 최대 | 실패율 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `ack-burst-20261009` | 같은 `event_id` | 100/20 | 1/99 | 66.783 req/s | 455.72ms | 538.44ms | 0% |
| `queue-recovery-20261009` | 고유 `event_id` | 5/5 | 5/0 | 165.635 req/s | 28.16ms | 28.28ms | 0% |

두 실행 모두 Check와 3초 이내 ACK 비율 100%를 확인했다. 중복 100건은 DB에 작업 한 건만 생성됐다. 5건 실행의 요청 처리량은 표본이 작아 100건 실행과 직접 비교하지 않는다.

### Queue 적체·처리 측정

Worker 중단 중 고유 작업 5건을 접수했다. 직전 중복 실험의 실제 작업 한 건이 처리 중 중단으로 재전달돼 Queue 최대 합계는 6이었으며 이 선행 작업을 결과에서 숨기거나 보정하지 않았다.

```text
표본: 247건, 1초 간격
최대 ready / unacknowledged / 합계: 6 / 1 / 6
관측 시작부터 Queue 0까지: 247.451초
고유 실험 작업 완료: 5/5
고유 작업 End-to-End p95: 227,505.44ms
고유 작업 최대 End-to-End: 236,618.78ms
고유 작업 처리량: 0.021131 jobs/s
```

Webhook ACK p95 28.16ms와 End-to-End p95 약 227.5초의 차이로 접수 경계와 단일 CPU Ollama 처리 병목이 분리돼 있음을 확인했다.

### Worker 중단 복구 측정

선행 작업은 `grade_documents` 중 Worker를 멈춰 DB `PROCESSING`, `attempt_count=1`에 남았다. 실험에서만 Lease를 10초, 재시도 기준을 1초로 주입하자 Recovery Scheduler가 `WORKER_LEASE_EXPIRED`를 기록하고 두 번째 시도로 재선점했다. PostgreSQL Checkpoint에서 `grade_documents`부터 재개해 분류·검색을 반복하지 않고 `COMPLETED`가 됐다.

복구 메시지가 Queue에 나타난 뒤 ACK돼 0이 되기까지 40.216초였고 최대 합계는 1이었다. 이후 Worker 환경은 `WORKFLOW_DOCUMENT_GRADER=ollama`, `WORKER_PROCESSING_LEASE_SECONDS=900`, `WORKER_RETRY_BASE_SECONDS=5`로 복원했다.

### 자동 검증

```text
docker compose --profile test run --build --rm -e WORKFLOW_DOCUMENT_GRADER=ollama test
결과: 106 passed in 4.40s

docker compose --profile test run --rm -e WORKFLOW_DOCUMENT_GRADER=ollama test python -m compileall -q app tests migrations
결과: 성공

docker compose --profile test run --rm -e WORKFLOW_DOCUMENT_GRADER=ollama test python -m pip check
결과: No broken requirements found.

docker compose --profile loadtest config --quiet
결과: 성공

docker compose --profile loadtest run --build --rm -e LOADTEST_QUEUE_MAX_SAMPLES=1 -e LOADTEST_QUEUE_OUTPUT_PATH=/results/observer-final-smoke.jsonl load-observer
결과: 실제 RabbitMQ 응답 검증·JSONL 저장 성공

git diff --check
결과: 성공
```

k6 스크립트는 실제 로컬 API에서 중복 10건 스모크, 중복 100건 Warm Run, 고유 5건 Queue Run으로 실행했다. 최종 콘솔 요약 출력도 기존 실행 ID의 중복 5건으로 다시 검증했다.

### 측정 중 발견한 조건

- 기존 로컬 `.env`의 폐기된 `WORKFLOW_DOCUMENT_GRADER=cloudflare_jev` 때문에 Compose가 API를 재생성한 첫 시도가 설정 검증에서 실패했다. 사용자 파일은 수정하지 않고 실행 프로세스에만 `ollama`를 주입했다.
- Queue가 0이어도 Worker 중단 당시 DB Lease가 남은 작업은 `PROCESSING`일 수 있었다. Queue와 PostgreSQL 최종 상태를 함께 봐야 한다는 원칙을 실제로 확인했고, 짧은 테스트 Lease로 자동 복구까지 완료했다.
- RabbitMQ `basic.get` 방식의 Worker라 관리 API `consumers`가 0으로 보일 수 있으므로 `ready`, `unacknowledged`와 DB 상태를 주된 판단 기준으로 사용한다.

### 남은 제한 사항과 다음 작업

- 실제 Slack PC 앱의 정상 Thread 답변과 사람 검토 전환 화면은 사용자 Workspace Token·Signing Secret 설정 뒤 직접 캡처해야 한다.
- 로컬 `.env`의 `WORKFLOW_DOCUMENT_GRADER`를 `ollama` 또는 `jev`로 사용자가 정리하지 않으면 다음 Compose 재생성 때 설정 검증이 실패한다.
- 모든 성능 결론은 WSL2 단일 로컬 Ollama CPU 환경에 한정한다.
- 10단계 PR 병합 뒤 실제 Slack 화면 두 장을 캡처하면 로드맵의 수동 증빙까지 끝난다.

## 2026-10-10 — 아키텍처·포트폴리오 문서와 실제 Slack 정상 E2E 정리

### 확인한 실제 흐름

- 무료 Slack Workspace에서 Events API Request URL 검증 완료
- 공개 테스트 채널에서 `app_mention` 수신 완료
- 로컬 API → Outbox → RabbitMQ → Worker → Ollama RAG 처리 완료
- 원본 Slack 메시지와 동일 Thread에 정상 답변 발신 완료
- 정상 질문으로 `정산 배치는 매일 몇 시에 결제 원장을 집계하나요?`를 사용했고 `22시` 답변을 확인

### 문서 산출물

- 전체 시스템, 내부 애플리케이션, Slack E2E, RAG Workflow, 비동기 복구, 로컬 실행·검증·관측 아키텍처 작성
- 여섯 다이어그램을 편집 가능한 draw.io XML과 SVG·PNG로 렌더링
- 실제 구현과의 연결 관계, 트랜잭션 경계, 동기·비동기 흐름과 프로덕션 운영이 아닌 로컬 검증 범위를 명시
- 포트폴리오 설명, 실제 측정 근거, 데모 순서와 캡처 기준을 `docs/PORTFOLIO.md`에 정리

### 남은 수동 증빙

- 사용자·Workspace 정보와 Tunnel 주소를 제거한 Slack 정상 Thread 공개용 캡처
- 문서에 없는 질문이 `REVIEW_REQUIRED`로 전환되는 실제 Slack·검토 화면
- 질문 3~5건 처리 직후 Grafana 대시보드 캡처

수동 화면 증빙 외 구현·자동 검증·아키텍처와 포트폴리오 설명 문서 작성은 완료했다.
