# slack-rag-harness 아키텍처 문서

이 디렉터리는 현재 저장소에 실제 구현된 구성만 코드·설정·기존 문서에서 추적해 정리한 시스템 아키텍처다. 읽는 순서는 **전체 구조 → 프로세스 내부 → 사용자 기능 → AI 의사결정 → 실패 복구 → 로컬 실행·검증**이다.

별도 웹 프론트엔드는 없다. 사용자의 주 화면은 Slack PC/Web 앱이고, 애플리케이션은 Slack Events API를 빠르게 접수한 뒤 RabbitMQ Worker에서 로컬 Ollama 기반 RAG를 비동기 실행한다. `/admin`과 Review/Recovery API는 선택형 로컬 개발·검증 접점이다. 이 문서는 프로덕션 배포 이력이 아니라 현재 Docker Compose 개발 환경에서 실제로 구현·검증한 구조를 설명한다.

## 권장 읽기 순서

| 순서 | 다이어그램 | 무엇을 이해하는가 | 원본 | 렌더링 |
|---|---|---|---|---|
| 1 | 전체 시스템 | 사용자 질문부터 Thread 답변·관측까지의 큰 흐름 | [draw.io](./01-system-overview.drawio) | [SVG](./01-system-overview.svg) · [PNG](./01-system-overview.png) |
| 2 | 내부 애플리케이션 | API, Outbox Publisher, Worker의 배포·데이터 경계 | [draw.io](./02-application-internals.drawio) | [SVG](./02-application-internals.svg) · [PNG](./02-application-internals.png) |
| 3 | Slack E2E | 3초 ACK 경계와 비동기 답변 발행 순서 | [draw.io](./03-slack-e2e-flow.drawio) | [SVG](./03-slack-e2e-flow.svg) · [PNG](./03-slack-e2e-flow.png) |
| 4 | RAG Workflow | 위험·검색 근거·인용 검증과 제한된 반복 | [draw.io](./04-rag-workflow.drawio) | [SVG](./04-rag-workflow.svg) · [PNG](./04-rag-workflow.png) |
| 5 | 비동기 복구 | 임대, 멱등 선점, Backoff, DLQ, Slack 재발행 | [draw.io](./05-async-recovery.drawio) | [SVG](./05-async-recovery.svg) · [PNG](./05-async-recovery.png) |
| 6 | 로컬 실행·검증·관측 | Docker Compose, 로컬 배치, 평가, 부하 테스트, 관측 스택 | [draw.io](./06-operations-batch.drawio) | [SVG](./06-operations-batch.svg) · [PNG](./06-operations-batch.png) |

자세한 구성요소 책임, 트랜잭션 경계, 동기/비동기 구분, 실패 처리와 코드 근거는 [ARCHITECTURE.md](./ARCHITECTURE.md)에 정리했다.

## 전체 시스템 한눈에 보기

![전체 시스템 아키텍처](./01-system-overview.svg)

## 표기 규칙

- 파란 실선: 동기 HTTP 또는 프로세스 내부 호출
- 보라 점선: 비동기 메시지, 외부 발행, 관측 데이터
- 초록 실선: 데이터베이스 조회·저장과 정상 상태 전이
- 주황/빨강 실선: 제한된 재시도, 사람 검토, 영구 실패
- `T1`, `T4` 등의 표기: 같은 DB 트랜잭션에서 원자적으로 확정해야 하는 경계

## 범위와 의도적 제외

- Cloudflare Quick Tunnel은 실제 Slack 로컬 E2E 문서에서 확인된 **개발 전용 전송 경로**로만 표시했다. 영구 운영 인프라로 해석하지 않는다.
- Kubernetes, 클라우드 DB, 관리형 Queue, 별도 API Gateway, 고정 Cron Scheduler는 코드와 설정에서 확인되지 않아 추가하지 않았다.
- `Jev`는 선택 실험 코드가 존재하지만 현재 기본 실행은 Ollama이며, 날짜 제한 때문에 주 런타임 아키텍처에는 넣지 않았다.
- 평가와 부하 테스트는 사용자 요청 경로가 아닌 개발자 호출형 로컬 검증 배치다.

