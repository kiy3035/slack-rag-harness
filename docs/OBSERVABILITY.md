# 로컬 관측 환경 가이드

## 1. 범위와 원칙

8단계 관측 환경은 업무 상태의 원장인 PostgreSQL을 바꾸지 않고 메트릭과 안전한 구조화 로그를 조회하는 로컬 보조 계층이다. Prometheus, Grafana, Loki, Alloy와 RabbitMQ Prometheus Plugin만 사용하며 유료 API나 관리형 SaaS에 의존하지 않는다.

구성 요소는 Docker Image와 Python Lock 파일에 버전을 고정했다. `prometheus-client`는 Apache-2.0/BSD-2-Clause, Prometheus와 Alloy는 Apache-2.0, Grafana와 Loki는 AGPL-3.0 라이선스의 무료 오픈소스 배포판을 로컬에서 사용한다.

## 2. 실행

```powershell
# 프로젝트 루트의 로컬 전용 .env에 필요한 값을 설정한다.
docker compose up --build -d
docker compose ps
```

기본 접속 주소는 다음과 같다.

| 화면·Endpoint | 주소 | 용도 |
| --- | --- | --- |
| 최소 관리 화면 | `http://localhost:8000/admin` | 최근 작업과 검토 대기 상태 |
| API 메트릭 | `http://localhost:8000/metrics` | API와 DB 집계 메트릭 |
| Grafana | `http://localhost:3000` | 통합 Dashboard와 Loki 검색 |
| Prometheus | `http://localhost:9090` | Target과 원시 PromQL 확인 |
| Loki 준비 상태 | `http://localhost:3100/ready` | 로그 저장소 준비 여부 |
| RabbitMQ 메트릭 | `http://localhost:15692/metrics` | Queue와 Broker 메트릭 |

관측 UI와 메트릭 포트는 Compose에서 `127.0.0.1`에만 바인딩한다. 다른 장치에서 접근해야 한다면 인증과 방화벽 정책을 먼저 구성한 뒤 바인딩을 의도적으로 변경한다.

Grafana의 Compose fallback 계정은 `admin` / `local_dev_password`다. 로컬 `.env`의 `GRAFANA_ADMIN_USER`, `GRAFANA_ADMIN_PASSWORD`로 덮어쓸 수 있으며, 외부 접근이 가능한 환경에서는 반드시 변경하고 별도 인증과 네트워크 접근 통제를 추가한다.

## 3. Dashboard

Grafana에 로그인하면 `Slack RAG Harness 관측` Dashboard가 자동으로 Provision된다. 포함된 패널은 다음과 같다.

- API Route별 요청량과 p95 응답시간
- 상태별 현재 작업 수
- `rag_harness.jobs`, Retry, DLQ Queue 적체량
- Workflow 전체와 노드별 p95 처리시간
- Recovery 재시도와 DLQ 발행 결과
- 사람 검토 대기량
- 임베딩·생성 등 Ollama 작업별 p95 지연
- 서비스별 Loki 로그

작업과 검토 Gauge는 `/metrics`를 읽을 때 PostgreSQL을 집계한다. 값이 없다는 이유로 업무 상태를 변경하거나 추정하지 않는다.

대시보드 시간대는 `Asia/Seoul`로 고정한다. 최소 관리 화면도 DB의 UTC 저장 시각을 `YYYY-MM-DD HH:MM:SS KST`로 변환해 표시한다. DB, API 응답, 구조화 로그, 재시도·Lease 계산은 비교 가능성과 복구 정합성을 위해 timezone-aware UTC를 유지한다.

## 4. 로그 검색과 개인정보 경계

API, Worker, Outbox Publisher 로그는 JSON 한 줄 형식으로 Loki에 전달된다. Grafana Explore에서 다음처럼 조회한다.

```logql
{service="worker"} | json
{service="worker"} | json | job_id="찾을-job-id"
{service=~"api|worker|outbox-publisher"} | json | level="ERROR"
```

기본 로그에는 `request_id`, `job_id`, `thread_id`, `workflow_node`, 안전한 상태와 오류 코드만 선택적으로 포함한다. 질문·답변·문서 본문·검토 의견·Token·비밀번호와 임의의 예외 원문은 기록하지 않는다. 최소 관리 화면도 식별자, 상태, 시도 횟수, 안전한 실패·검토 사유 코드와 시각만 표시한다.

## 5. 설정

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `METRICS_ENABLED` | `true` | 프로세스 메트릭 서버 활성화 |
| `METRICS_PORT` | 서비스별 지정 | Worker `9101`, Outbox `9102` |
| `ENABLE_ADMIN_OBSERVABILITY` | Compose에서 `true` | 인증 없는 로컬 관리 화면 활성화 |
| `LOG_DIRECTORY` | `/var/log/slack-rag` | Alloy가 읽는 구조화 로그 경로 |
| `GRAFANA_ADMIN_USER` | `admin` | 로컬 Grafana 관리자 이름 |
| `GRAFANA_ADMIN_PASSWORD` | `local_dev_password` | 로컬 예시 비밀번호 |
| `PROMETHEUS_PORT` | `9090` | 호스트 Prometheus 포트 |
| `GRAFANA_PORT` | `3000` | 호스트 Grafana 포트 |
| `LOKI_PORT` | `3100` | 호스트 Loki 포트 |
| `RABBITMQ_PROMETHEUS_PORT` | `15692` | 호스트 RabbitMQ 메트릭 포트 |

API 설정의 안전한 기본값은 `ENABLE_ADMIN_OBSERVABILITY=false`다. Compose 로컬 개발 환경만 명시적으로 활성화한다. 인증 계층을 추가하기 전에는 관리 화면과 관측 포트를 공용 네트워크에 공개하지 않는다.

## 6. 검증

```powershell
docker compose config --quiet
Invoke-RestMethod http://localhost:8000/health/ready
Invoke-WebRequest http://localhost:8000/metrics
Invoke-RestMethod http://localhost:9090/api/v1/targets
Invoke-RestMethod http://localhost:3000/api/health
Invoke-WebRequest http://localhost:3100/ready
Invoke-WebRequest http://localhost:15692/metrics
```

Prometheus Targets에서 `slack-rag-api`, `slack-rag-worker`, `slack-rag-outbox`, `rabbitmq`가 모두 `up`이어야 한다. Loki는 컨테이너 시작 직후 Ring 안정화를 위해 잠시 `not ready`를 반환할 수 있으므로 서비스가 계속 실행 중인지 확인한 뒤 다시 조회한다.

자동 검증은 실제 PostgreSQL과 RabbitMQ를 사용하되 Grafana 화면 접속이나 Slack 연결은 요구하지 않는다.

```powershell
docker compose --profile test run --build --rm test
```

## 7. 문제 해결

- Grafana에 Dashboard가 없으면 `docker compose logs grafana`에서 Provisioning 오류를 확인한다.
- Prometheus 패널이 비어 있으면 `http://localhost:9090/targets`에서 Target 상태와 마지막 오류를 확인한다.
- RabbitMQ Queue 패널이 비어 있으면 `rabbitmq_prometheus` Plugin과 `15692/metrics` 응답을 확인한다.
- Loki 로그가 비어 있으면 API나 Worker에 요청을 발생시킨 뒤 `docker compose logs alloy loki`를 확인한다.
- 관리 화면이 `404`이면 `ENABLE_ADMIN_OBSERVABILITY`가 `true`인지 확인한다. 외부 환경에서 이를 켜는 것으로 접근 통제를 대신하면 안 된다.
