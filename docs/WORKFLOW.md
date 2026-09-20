# LangGraph 기본 Workflow

## 범위

4단계는 RabbitMQ Worker가 선점한 작업을 다음 순서로 처리한다.

1. 질문 공백 정규화와 최대 길이 검증
2. Ollama 구조화 출력으로 의도와 위험 등급 분류
3. `nomic-embed-text`와 pgvector를 사용한 현재 문서 Chunk 검색
4. 검색된 Chunk만 Context로 전달한 구조화 답변 생성

각 노드는 시작, 완료, 실패와 수행 시간을 기록한다. 질문 전문과 문서 전문은 로그에 남기지 않는다. LangGraph 상태에는 문자열, 숫자, 배열, 객체처럼 Checkpoint로 직렬화할 수 있는 값만 저장하며 DB Session과 HTTP Client는 저장하지 않는다.

## 로컬 모델

Embedding과 생성은 모두 호스트에서 실행 중인 Ollama를 사용한다.

```powershell
ollama pull nomic-embed-text
ollama pull qwen3:1.7b
ollama list
```

Compose 내부에서는 `http://host.docker.internal:11434`로 Ollama에 연결한다. 모델명은 각각 `OLLAMA_EMBEDDING_MODEL`, `OLLAMA_GENERATION_MODEL`로 바꿀 수 있다. 생성 응답은 Pydantic JSON Schema를 Ollama `format`에 전달하고 `temperature=0`으로 요청한다.

## Checkpoint와 재개

`AsyncPostgresSaver`가 사용하는 테이블은 Worker 시작 시 멱등적으로 준비된다. `thread_id`는 Slack Thread가 있으면 해당 값, 아니면 `job_id`를 사용한다. 같은 `thread_id`로 다시 실행하면 완료된 앞 노드를 반복하지 않고 마지막 미완료 노드부터 이어간다.

통합 테스트는 답변 생성 노드에서 첫 Worker 인스턴스를 실패시킨 뒤 Checkpointer 연결과 그래프를 새로 만들었다. 두 번째 인스턴스가 같은 작업을 재개할 때 의도 분류와 검색은 반복하지 않고 생성 노드만 다시 실행하는지 실제 PostgreSQL Checkpoint로 검증한다.

이 검증은 Workflow 재개 경계를 보장하지만 RabbitMQ의 자동 장애 복구 전체를 뜻하지 않는다. 프로세스가 강제 종료돼 작업이 `PROCESSING`에 남은 경우 재전달 메시지를 언제 다시 선점할지는 Lease와 오류 분류가 필요한 6단계 범위다.

## 안전 경계

- 중간 이상 위험 질문은 검색·생성을 실행하지 않고 `REVIEW_REQUIRED`로 종료한다.
- 검색 결과가 없으면 사내 절차를 만들지 않고 `REVIEW_REQUIRED` 답변을 만든다.
- 정상 답변은 이번 실행에서 검색된 `(document_id, chunk_id)` 쌍만 인용할 수 있다.
- Ollama timeout과 HTTP 실패, 구조화 출력 위반, 허용되지 않은 인용은 성공으로 저장하지 않는다.
- 관련성 판정, 검색어 재작성, 재생성 제한, `review_queue`, 승인·반려 API는 5단계에서 구현한다.

## 실행

생성 모델이 준비된 뒤 Worker를 포함해 서비스를 시작한다.

```powershell
docker compose up --build -d postgres rabbitmq api outbox-publisher worker
docker compose logs -f worker
```

`POST /api/v1/events`로 접수한 작업은 Outbox Publisher가 RabbitMQ에 발행하고 Worker가 처리한다. 현재 조회 API는 작업 상태와 오류만 노출하며 답변 조회·Slack 발신 계약은 후속 단계에서 확정한다.
