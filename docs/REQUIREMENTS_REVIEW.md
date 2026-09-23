# 요구사항 검토와 0·1단계 결정

검토일: 2026-09-19

## 충돌과 기술 보정

1. `01_아키텍처_및_흐름.md`는 `url_verification` challenge 반환을 서명 검증보다 먼저 배치했지만, 이 요청도 Slack 서명 대상이다. 원본 Body의 Timestamp와 HMAC을 먼저 검증한 뒤 JSON을 파싱하고 challenge를 반환하도록 순서를 수정했다.
2. 로드맵 1단계와 7단계에 Slack URL 검증과 서명 검증이 중복돼 있다. 1단계는 수신 계약·보안 경계·Fixture 기반 멱등 저장까지, 7단계는 Cloudflare Tunnel·실제 Slack 발신·Workspace E2E로 경계를 명시했다.
3. 원본 `event.ts`만 `chat.postMessage.thread_ts`로 쓰라는 문구는 기존 Thread 안의 멘션에서 부모 Thread를 잃을 수 있다. `event.thread_ts`가 있으면 유지하고, 최상위 메시지일 때만 `event.ts`를 쓰도록 보정했다.
4. 0단계는 PostgreSQL과 RabbitMQ만 요구하지만 `AGENTS.md`는 Prometheus, Grafana, Loki도 Compose로 실행하라고 한다. 로드맵의 단계 제한을 우선해 0·1단계에는 PostgreSQL+pgvector와 RabbitMQ만 포함하고 관측 스택은 8단계에서 추가한다. 이는 도구 선택을 취소한 것이 아니라 구현 시점을 제한한 것이다.
5. LangGraph Checkpointer만으로 Worker 강제 종료 복구가 자동 보장되지는 않는다. 4단계는 새 Worker 인스턴스가 같은 `thread_id`로 실패한 노드부터 재개하는 경계를 검증했다. RabbitMQ 재전달과 `PROCESSING` Lease를 결합한 자동 복구는 6단계에서 완성해야 한다.
6. RabbitMQ와 외부 Slack 전송은 정확히 한 번 처리를 제공하지 않는다. 문서가 이미 최소 한 번 전달과 Slack 전송 직후 종료 구간의 중복 가능성을 인정하므로, 1단계에서는 DB UNIQUE 제약과 동일 작업 ID 반환을 최종 방어선으로 삼았다.
7. Slack의 3초 제한은 LLM 처리 완료 제한이 아니라 이벤트 수신 ACK 제한이다. 1단계 API는 DB의 작업+Outbox 저장까지만 수행하고 LLM과 메시지 발행은 호출하지 않는다.

## 0·1단계 기술 결정

- Python 3.13, FastAPI, Pydantic v2, SQLAlchemy async, asyncpg, Alembic을 사용한다.
- PostgreSQL 17+pgvector와 RabbitMQ 4.3을 Docker Compose에서 고정 태그로 실행한다.
- 로컬 요청은 `LOCAL`, Slack 요청은 `SLACK` source로 분리하고 `(source, external_event_id)` UNIQUE 제약을 둔다.
- 중복 로컬 요청은 기존 `job_id`, `duplicate=true`, HTTP 202를 반환한다.
- 중복 Slack 요청은 기존 `job_id`, `duplicate=true`, HTTP 200으로 ACK한다.
- 신규 작업 상태는 `RECEIVED`, Outbox 상태는 `READY`다. Outbox Publisher가 생기는 2단계 전에는 `QUEUED`로 미리 표시하지 않는다.
- `POST /api/v1/events`는 `ENABLE_LOCAL_EVENTS=true`일 때만 노출한다.
- 작업 조회 응답에는 질문 원문을 포함하지 않아 불필요한 개인정보 노출을 줄인다.

## 무료 여부와 라이선스

추가한 런타임·테스트 라이브러리는 모두 무료 오픈소스다. FastAPI, Pydantic, SQLAlchemy, Alembic, pytest, LangGraph와 LangGraph PostgreSQL Checkpointer는 MIT 계열 라이선스이고 asyncpg와 aio-pika는 Apache-2.0, Uvicorn과 HTTPX는 BSD-3-Clause 계열이다. PostgreSQL과 pgvector는 PostgreSQL License, RabbitMQ 서버는 MPL-2.0이다. LLM과 Embedding은 호스트의 로컬 Ollama만 사용하며 유료 API나 외부 관리형 서비스를 추가하지 않았다.

## 4단계와 5단계 경계

- 4단계 검증 조건인 “최종 답변에 실제 검색 Chunk ID 포함”과 AI 하네스 규칙을 만족하려고, 현재 실행에서 검색한 ID 집합 밖의 인용을 거부하는 최소 안전 검사를 4단계에 포함했다.
- 관련성 재판정, 검색어 재작성, 답변 재생성 횟수 제한, 인용 영속화, 사람 승인 API는 로드맵에 명시된 5단계이므로 구현하지 않았다.
- 따라서 4단계의 `REVIEW_REQUIRED`는 작업 상태까지만 기록한다. 별도 `review_queue`와 승인 흐름은 5단계에서 추가한다.

## 5단계 기술 결정

- Slack `thread_id`를 LangGraph Checkpoint 키로 그대로 쓰면 같은 Thread의 여러 작업이 상태를 공유한다. Checkpoint는 `{job_id}:run:{workflow_revision}`으로 분리했다.
- 재검색 요청은 새 Outbox를 만들지 않는다. 작업 세대를 한 번 증가시키고 기존의 작업별 UNIQUE Outbox를 `READY`로 되돌려 중복 발행 행을 방지한다.
- LangGraph `interrupt`는 선택 사항이며, 이번 단계는 DB의 `review_queue`를 지속 가능한 검토 원장으로 사용한다. 검토 재검색은 새 Workflow 세대로 시작해 과거 완료 상태를 잘못 재사용하지 않는다.
- 근거가 없는 민감 질문은 승인 버튼만으로 완료할 수 없다. 초안과 허용된 관련 인용이 모두 있는 경우에만 승인·수정 승인을 허용한다.
- LLM이 반환한 단일 신뢰도에 의존하지 않고 검색 유사도, 관련 Chunk ID, 문서 충돌, Schema 결과, 인용 허용목록을 각각 보존하고 검사한다.
- 실제 `qwen3:1.7b`가 문서 밖 질문에도 관련 Chunk를 선택한 사례가 있어, 모델 판정 뒤 질문 핵심어와 문서의 결정적 중첩을 추가 검사한다. 의미상 동의어를 놓치는 경우에는 근거 없이 완료하지 않고 제한 재검색 뒤 검토로 보낸다.

## 5A단계 Jev 실험의 요구사항 충돌과 결정

- Jev는 Vercel AI Gateway를 거치는 외부 모델이므로 프로젝트의 영구 로컬 실행 원칙을 충족하지 않는다. 핵심 Workflow의 기본값과 자동 테스트는 Ollama로 유지하고, Jev를 5단계 관련성 판정의 선택형 비교 실험으로만 추가한다.
- 2026-09-23 확인 시 공식 모델 페이지는 Jev를 무료로 표시하지만 프로모션 종료일을 2026-09-25로 명시한다. 영구 무료로 표현하지 않고 해당 날짜 이후 호출을 코드에서 차단하며, 환경변수만으로 확인된 종료일을 늦출 수 없게 한다.
- 월 $5 무료 크레딧은 일부 무료 대상 모델에만 적용되며 Jev가 프로모션 후에도 포함된다는 보장은 없다. 종료일을 연장하기 전 공식 가격을 다시 확인해야 한다.
- Jev로 전송되는 상태에는 질문과 검색 Chunk 본문이 포함된다. 저장소의 가상 매뉴얼에만 사용하며 실제 회사 문서, 고객 정보, 개인정보에는 사용하지 않는다.
- Jev 확률만으로 자동 완료를 결정하지 않는다. 검색 집합 ID, 질문 핵심어, 인용 Schema 검증은 기존 코드가 계속 수행하고 외부 장애 시 로컬 Ollama 판정으로 폴백한다.

직접 의존성은 `pyproject.toml`, 해석된 전체 의존성은 `requirements.lock`, Docker 이미지 태그는 `Dockerfile`과 `compose.yaml`에 고정했다. 새 버전 도입 시 같은 통합 테스트를 다시 실행한다.
