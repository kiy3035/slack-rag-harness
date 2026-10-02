# Slack RAG Harness

무료·로컬 실행을 우선하는 비동기 AI 하네스다. 현재 구현 범위는 로드맵 0단계부터 9단계까지이며, 관련성 판정·제한 재검색·출력 검증, 멱등적인 사람 검토, Worker 장애 복구, 실제 Slack Thread 발신, 로컬 관측 환경과 고정 평가 하네스를 포함한다.

## 실행

Docker Desktop과 Docker Compose가 필요하다. 호스트 Python은 필요하지 않다.

직접 의존성은 `pyproject.toml`, 해석된 전체 의존성은 `requirements.lock`에 고정돼 있다.

```powershell
Copy-Item .env.example .env
docker compose up --build -d
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

`worker`는 메시지를 조건부 선점한 뒤 LangGraph Workflow를 실행한다. 일시 오류는 DB에 Backoff 시각을 저장해 제한 재시도하고, 출력 계약 오류는 사람 검토로, 영구 오류와 재시도 소진은 DLQ로 분리한다. 로컬 생성 모델이 준비되기 전에 작업을 소비하지 않도록 아래의 모델 설치를 먼저 끝내고 서비스를 시작한다.

## 문서 적재와 검색

`knowledge/manuals`의 가상 운영 매뉴얼 6개를 제목·문단 기준으로 분할하고, 로컬 Ollama의 `nomic-embed-text` 임베딩을 PostgreSQL `vector(768)`에 저장한다. Nomic 검색 계약에 따라 문서에는 `search_document:`, 질문에는 `search_query:` 접두어를 적용한다. 같은 문서 원문과 임베딩 설정은 재적재하지 않으며 내용·모델·접두어가 바뀌면 버전을 올리고 기존 Chunk를 원자적으로 교체한다.

```powershell
ollama pull nomic-embed-text
docker compose run --rm api python -m app.retrieval.main ingest knowledge/manuals
docker compose run --rm api python -m app.retrieval.main search "정산 배치 마감 전에 무엇을 확인하나요?"
```

설정, 재적재 정책, 검색 임계값에 대한 설명은 [문서 적재와 검색 가이드](docs/RETRIEVAL.md)에 정리했다.

## 검증·재검색 답변 Workflow

5단계 Workflow는 입력 검증, 의도 분류, pgvector 검색, 관련성 판정, 최대 1회 검색어 재작성, 구조화 답변 생성, 최대 2회 출력·인용 검증을 수행한다. 각 노드 결과는 PostgreSQL에 Checkpoint로 저장하고 생성 모델은 무료 로컬 Ollama 모델만 사용한다.

```powershell
ollama pull qwen3:1.7b
docker compose up --build -d postgres rabbitmq api outbox-publisher worker
```

정상 답변은 이번 실행에서 관련성이 통과된 `document_id`와 `chunk_id`만 인용할 수 있다. 민감 질문, 문서 충돌, 근거 부족, 출력 계약 위반은 자동 완료하지 않고 `review_queue`에 남긴다. 구성과 재개 범위는 [Workflow 가이드](docs/WORKFLOW.md), 검토 절차는 [사람 검토 API 가이드](docs/REVIEW.md)에 정리했다.

### 선택형 Jev 관련성 실험

기본 관련성 판정은 계속 로컬 Ollama를 사용한다. 가상 문서로 Jev를 비교할 때만 `.env`의 `WORKFLOW_DOCUMENT_GRADER=jev`와 `AI_GATEWAY_API_KEY`를 설정한다. Jev는 답변을 생성하거나 DB를 변경하지 않고 pgvector가 검색한 Chunk의 직접 관련성과 문서 충돌 확률만 판정한다. 호출 실패 시 기본 설정에서는 로컬 Ollama로 폴백한다.

Vercel이 공지한 Jev 무료 프로모션은 2026-09-25 종료 예정이므로 코드가 그 이후 외부 호출을 차단한다. 이 날짜 이후의 가격과 무료 대상 여부를 다시 확인하기 전에는 날짜를 연장하지 않는다. 실제 회사 문서나 개인정보는 보내지 않는다. 설정과 데이터 경계는 [Jev 선택형 실험 가이드](docs/JEV_EXPERIMENT.md)에 정리했다.

## 사람 검토

대기 항목은 `GET /api/v1/reviews?status=WAITING`과 `GET /api/v1/reviews/{review_id}`로 조회한다. 검토자는 원 검색 근거를 확인한 뒤 승인, 수정 승인, 재검색, 반려 중 하나를 선택한다. 동일 결정을 다시 보내도 한 번만 적용된다.

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/reviews/{review_id}/reject -ContentType application/json -Body '{"comment":"근거 부족"}'
```

## 장애 복구

Worker의 Ollama·DB 일시 오류는 `RETRY_WAIT`에 다음 재시도 시각을 저장하고 Recovery Scheduler가 기존 Outbox를 다시 연다. 강제 종료로 오래 남은 `PROCESSING`도 Lease 만료 후 같은 경로로 복구한다. 영구 오류와 재시도 소진은 `DEAD_LETTER` 상태와 DLQ 발행 완료 시각을 함께 남긴다.

실패 작업의 관리자 수동 재처리 Endpoint는 기본 비활성화이며 로컬에서 `ENABLE_ADMIN_RECOVERY=true`를 설정한 경우에만 사용할 수 있다. 오류 분류, 상태 전이, 환경변수, 장애별 재현 결과는 [Worker 장애 복구 가이드](docs/RECOVERY.md)에 정리했다.

## 실제 Slack 연동

Slack Events API는 서명 검증과 중복 방지 후 3초 안에 ACK하고, AI 처리는 기존 RabbitMQ Worker가 수행한다. 자동 완료 답변과 사람이 승인한 답변은 작업 완료 트랜잭션에서 `slack_reply_outbox`에 한 번 예약된다. Worker의 Slack Publisher는 원본 `channel`과 부모 `thread_ts`로 `chat.postMessage`를 호출한다.

실제 Workspace를 연결할 때만 `.env`에 `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_REPLY_ENABLED=true`를 설정한다. Slack API 429의 `Retry-After`와 일시 오류는 제한 재시도하며, 영구 오류나 재시도 소진은 `FAIL` 상태와 안전한 오류 코드로 남는다. App 생성, Scope, Event Subscription, 무료 Quick Tunnel과 화면 E2E 절차는 [Slack 설정 가이드](docs/SLACK_SETUP.md)에 정리했다.

## 관측 화면

전체 Compose를 시작하면 Prometheus가 API·Worker·Outbox Publisher·RabbitMQ를 수집하고, Grafana의 `Slack RAG Harness 관측` Dashboard에서 요청량과 p95, 상태별 작업 수, Queue 적체, Workflow·Ollama 지연, 재시도·DLQ, 검토 대기량을 확인할 수 있다. Alloy는 질문·답변 원문을 제외한 JSON 로그를 Loki로 전달한다.

- 최소 관리 화면: `http://localhost:8000/admin`
- Grafana: `http://localhost:3000`
- Prometheus: `http://localhost:9090`

관리 화면은 로컬 Compose에서만 기본 활성화하며 인증 기능이 없다. 외부에 공개하지 말고 운영 Profile에서는 `ENABLE_ADMIN_OBSERVABILITY=false`를 유지한다. 실행·검색·보안·문제 해결 절차는 [관측 환경 가이드](docs/OBSERVABILITY.md)에 정리했다.

## 평가 하네스

60건 JSONL 데이터셋은 답변 가능, 문서 없음, 표현 변형, 다중 문서, 문서 충돌, 민감 작업을 각각 10건씩 고정한다. 실제 pgvector 검색과 로컬 Ollama Workflow를 실행해 Retrieval Recall@K, 인용 정확도, 검토 전환 정확도, 근거 없는 문장률, 처리량과 p95를 JSON·Markdown으로 저장한다. 평가 실행은 인메모리 Checkpoint를 사용하며 운영 작업과 검토 테이블을 변경하지 않는다.

```powershell
docker compose run --rm api python -m app.evaluation.main validate
docker compose run --rm api python -m app.evaluation.main run --top-k 5 --min-score -1.0 --max-query-rewrites 1 --worker-count 1
```

결과는 `evaluation/results`에 생성되며 Git에 포함되지 않는다. Top-K, 유사도 임계값, 재작성 횟수, Worker 수는 기준 실행에서 한 조건씩만 바꿔 비교한다. 지표 정의와 비교 명령은 [평가 하네스 가이드](docs/EVALUATION.md)에 정리했다.

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
- Bot Token은 Slack Web API의 Authorization Header로만 전달하고 로그에 남기지 않는다.
- Slack 답변의 링크·미디어 미리보기는 외부 콘텐츠 자동 노출을 줄이기 위해 비활성화한다.
- 운영 Profile에서는 `ENABLE_LOCAL_EVENTS=false`로 로컬 우회 Endpoint를 숨긴다.
- 운영 공개 전에는 `ENABLE_ADMIN_RECOVERY=false`를 유지하고 관리자 인증·권한 계층을 추가한다.
- 운영 공개 전에는 `ENABLE_ADMIN_OBSERVABILITY=false`를 유지하고 Grafana와 관리 화면에 별도 접근 통제를 적용한다.

요구사항 충돌과 결정 근거는 [docs/REQUIREMENTS_REVIEW.md](docs/REQUIREMENTS_REVIEW.md)에 기록했다.
