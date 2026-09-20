# LangGraph 검증·재검색 Workflow

## 범위

5단계는 RabbitMQ Worker가 선점한 작업을 다음 순서로 처리한다.

1. 질문 공백 정규화와 최대 길이 검증
2. Ollama 구조화 출력으로 의도와 위험 등급 분류
3. `nomic-embed-text`와 pgvector를 사용한 현재 문서 Chunk 검색
4. 검색 점수와 분리된 문서 관련성·충돌 판정
5. 근거가 부족하면 최대 1회 검색어 재작성 후 재검색
6. 관련성이 통과된 Chunk만 Context로 전달한 구조화 답변 생성
7. Schema·인용 허용목록·민감 패턴 검증과 최대 2회 생성 제한
8. 자동 완료할 수 없는 결과를 `review_queue`에 저장

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

`AsyncPostgresSaver`가 사용하는 테이블은 Worker 시작 시 멱등적으로 준비된다. Checkpoint `thread_id`는 Slack 대화 ID와 분리한 `{job_id}:run:{workflow_revision}`을 사용한다. 같은 Slack Thread의 서로 다른 질문이 상태를 공유하지 않으며, 검토자가 재검색을 요청하면 `workflow_revision`을 한 번 올려 새 실행을 시작한다. 같은 실행을 다시 처리할 때는 완료된 앞 노드를 반복하지 않고 마지막 미완료 노드부터 이어간다.

통합 테스트는 답변 생성 노드에서 첫 Worker 인스턴스를 실패시킨 뒤 Checkpointer 연결과 그래프를 새로 만들었다. 두 번째 인스턴스가 같은 작업을 재개할 때 의도 분류와 검색은 반복하지 않고 생성 노드만 다시 실행하는지 실제 PostgreSQL Checkpoint로 검증한다.

이 검증은 Workflow 재개 경계를 보장하지만 RabbitMQ의 자동 장애 복구 전체를 뜻하지 않는다. 프로세스가 강제 종료돼 작업이 `PROCESSING`에 남은 경우 재전달 메시지를 언제 다시 선점할지는 Lease와 오류 분류가 필요한 6단계 범위다.

## 안전 경계

- 중간 이상 위험 질문은 검색·생성을 실행하지 않고 `REVIEW_REQUIRED`로 종료한다.
- 위험도는 질문에 민감한 단어가 있는지가 아니라 실제 변경·승인·삭제·발송 실행을 요구하는지로 판단한다. 절차나 정책을 읽기 전용으로 묻는 질문은 실행 요청과 구분한다.
- 검색 결과가 없으면 사내 절차를 만들지 않고 `REVIEW_REQUIRED` 답변을 만든다.
- 정상 답변은 이번 실행에서 검색된 `(document_id, chunk_id)` 쌍만 인용할 수 있다.
- 검색 점수, 관련성 판정, Schema 검증, 인용 검증 결과를 서로 분리해 기록한다.
- 작은 로컬 모델의 과잉 관련성 판정을 그대로 신뢰하지 않고, LLM이 선택한 Chunk 중 질문의 비일반 핵심어가 실제 제목·소제목·본문에 겹치는 근거만 사용한다. 이 보수적 검사는 의미상 동의어를 놓칠 수 있으므로 자동 완료 대신 재검색·사람 검토로 안전하게 전환한다.
- 검색어 재작성은 기본 1회, 답변 생성은 기본 2회로 제한해 무한 재시도를 막는다.
- Ollama timeout과 HTTP 실패, 구조화 출력 위반, 허용되지 않은 인용은 성공으로 저장하지 않는다.
- `review_queue` 결정은 행 잠금과 조건부 상태 변경으로 중복 적용을 막는다.

## 실행

생성 모델이 준비된 뒤 Worker를 포함해 서비스를 시작한다.

```powershell
docker compose up --build -d postgres rabbitmq api outbox-publisher worker
docker compose logs -f worker
```

`POST /api/v1/events`로 접수한 작업은 Outbox Publisher가 RabbitMQ에 발행하고 Worker가 처리한다. 검토 API는 [사람 검토 API 가이드](REVIEW.md)를 따른다. 현재 작업 조회 API는 상태와 오류만 노출하며 Slack 발신 계약은 후속 단계에서 확정한다.
