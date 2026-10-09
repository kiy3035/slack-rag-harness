# 블로그 초안 — 빠른 챗봇보다 실패해도 이어지는 AI 하네스

## 한 줄 요약

이 프로젝트는 Slack 질문에 답하는 기능보다, LLM 작업을 빠르게 접수한 뒤 검색 근거를 검증하고 중복·장애·중단에서도 같은 작업을 안전하게 이어가는 구조를 구현했다.

## 왜 비동기로 만들었나

로컬 `qwen3:1.7b` 생성은 수십 초가 걸릴 수 있지만 Slack Events API는 3초 안에 응답해야 한다. 그래서 API는 Slack 서명 검증과 PostgreSQL 작업·Outbox 저장까지만 끝낸 뒤 ACK한다. Outbox Publisher가 RabbitMQ에 `job_id`를 보내고 Worker가 LangGraph, pgvector와 Ollama를 실행한다.

이 분리 덕분에 LLM이 느리거나 Worker가 잠시 중단돼도 Slack은 요청을 재전송하느라 폭주하지 않고, 저장된 작업은 Queue와 DB 상태를 기준으로 다시 처리할 수 있다.

## 중복과 유실을 어떻게 막았나

- `source + external_event_id` DB UNIQUE 제약으로 Slack 재전송을 원자적으로 막았다.
- 작업과 Outbox를 한 트랜잭션으로 저장해 “DB에는 작업이 있지만 Queue에는 없는” 구간을 복구 가능하게 만들었다.
- Worker는 이전 상태를 조건으로 선점하고 영향 행 수를 확인한다.
- RabbitMQ ACK는 처리 결과가 안전하게 저장된 뒤에만 수행한다.
- 일시 오류만 제한적으로 재시도하고 소진 시 DB 상태와 DLQ를 함께 남긴다.

## RAG 답변을 왜 다시 검증했나

LLM이 반환한 자신감 하나를 믿지 않았다. pgvector 검색 점수, Chunk 관련성, 문서 충돌, Pydantic Schema, 이번 실행에서 검색한 Chunk ID인지 여부를 각각 확인했다. 근거가 없거나 민감한 작업이면 일반 지식으로 사내 절차를 만들어내지 않고 사람 검토로 보낸다.

## 실제 측정에서 배운 점

WSL2 Linux x86_64, 논리 CPU 16개, `qwen3:1.7b`, `nomic-embed-text`, 고정 데이터셋 60건으로 검색 조건을 한 번에 하나씩 바꿨다.

| 조건 | Recall@K | 처리량(cases/s) | p95 | 실패 |
| --- | ---: | ---: | ---: | ---: |
| Top-K 5 기준 | 74.00% | 0.019200 | 91,920.22ms | 0 |
| Top-K 3 | 67.00% | 0.019956 | 79,493.51ms | 1 |
| Top-K 8 | 81.00% | 0.015627 | 110,563.45ms | 1 |
| 재작성 없음 | 70.00% | 0.019936 | 91,004.60ms | 1 |

Top-K 8은 Recall이 높았지만 더 느리고 Timeout이 생겼다. Worker 2개도 Worker 1개보다 빨라지지 않았다. 단일 CPU Ollama가 병목이어서 처리량은 약 5.0% 낮고 p95는 약 90.6% 높았다. 따라서 현재 기본값은 Top-K 5, 재작성 1회, Worker 1개다.

Webhook 접수는 같은 Slack `event_id`를 100회, VU 20으로 보낸 실제 로컬 k6 실행에서 100건 모두 성공했다. p95는 455.72ms, 최대 538.44ms, 실패율 0%였고 DB에는 작업 1건만 생성됐다. 이 수치는 답변 생성 속도가 아니라 서명 검증·멱등 저장·ACK 경계의 성능이다.

## 장애를 화면으로 설명하는 법

Worker를 멈춘 상태에서 고유 Webhook을 보내면 API는 계속 ACK하고 RabbitMQ `ready`가 증가한다. Worker를 다시 시작하면 `ready`가 줄고 처리 중인 한 건은 `unacknowledged`로 보인다. 마지막 메시지가 안전하게 저장되고 ACK되면 Queue 합계가 0이 된다. Grafana의 Queue depth와 작업 상태 패널, JSONL 타임라인을 같이 보면 “요청이 사라진 것이 아니라 기다렸다가 처리됐다”는 점을 설명할 수 있다.

실제 Worker 1개 실험에서는 고유 작업 5건과 중단 시 재전달된 선행 작업 1건으로 Queue 합계가 최대 6까지 올라갔다가 247.451초 뒤 0이 됐다. 고유 작업 5건은 모두 완료됐고 End-to-End p95는 227,505.44ms, 처리량은 0.021131 jobs/s였다. 같은 실행의 Webhook ACK p95는 28.16ms였으므로 빠른 접수와 느린 로컬 추론이 분리됐음을 수치로 확인할 수 있다.

선행 작업은 `PROCESSING`에서 Worker가 중단된 실제 복구 사례가 됐다. 테스트에서만 Lease를 10초로 줄이자 Recovery Scheduler가 `WORKER_LEASE_EXPIRED`와 두 번째 시도를 기록했고, LangGraph는 완료된 분류·검색을 반복하지 않고 `grade_documents` Checkpoint부터 재개했다. 복구 메시지는 40.216초 뒤 ACK됐고 작업은 `COMPLETED`가 됐다. 이후 Lease는 기본 900초로 복원했다.

## 한계와 다음 개선

- 성능 결론은 WSL2 단일 CPU Ollama 환경에 한정된다.
- 생성 결과는 완전히 결정적이지 않아 작은 품질 차이는 반복 실행으로 분산을 확인해야 한다.
- 현재 검색 최소 점수 0.2는 60건 모두 기준과 같은 Chunk를 반환해 임계값 개선 근거가 되지 못했다.
- Slack 성공 직후 DB 기록 전에 Worker가 종료되면 같은 `client_msg_id` 재호출 가능성이 남는다.
- 실제 Slack PC 앱의 정상 Thread 답변과 검토 전환 화면은 사용자 무료 Workspace에서 마지막으로 캡처해야 한다.
- 다음 성능 개선 후보는 Worker 수 증가보다 모델 양자화 비교, GPU 사용 또는 Ollama 인스턴스 분리다. 바꾸기 전후에는 같은 평가 데이터셋과 부하 조건을 사용해야 한다.

## 면접에서 설명할 핵심

“Slack 챗봇을 만들었다”보다 다음 흐름으로 설명한다.

1. 3초 ACK와 느린 LLM 처리의 시간 제약을 분리했다.
2. DB UNIQUE, Transactional Outbox, 조건부 선점으로 중복과 유실을 방어했다.
3. 검색 근거와 인용을 LLM 밖에서 검증하고 불확실하면 사람에게 넘겼다.
4. Checkpoint, Lease, 제한 재시도와 DLQ로 중단 후 복구 경로를 만들었다.
5. 같은 데이터셋과 한 번에 하나의 조건 변경으로 품질·성능을 실제 측정했다.
