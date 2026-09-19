# Slack RAG Harness

무료·로컬 실행을 우선하는 비동기 AI 하네스다. 현재 구현 범위는 로드맵 0단계와 1단계이며, LLM·검색·RabbitMQ 발행·실제 Slack 발신은 아직 포함하지 않는다.

## 실행

Docker Desktop과 Docker Compose가 필요하다. 호스트 Python은 필요하지 않다.

직접 의존성은 `pyproject.toml`, 해석된 전체 의존성은 `requirements.lock`에 고정돼 있다.

```powershell
Copy-Item .env.example .env
docker compose up --build -d postgres rabbitmq api
docker compose ps
Invoke-RestMethod http://localhost:8000/health/ready
```

로컬 이벤트 접수:

```powershell
$body = @{ external_event_id = "local-demo-1"; question = "정산 배치 처리 방법은?" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/events -ContentType application/json -Body $body
```

반환된 `job_id`는 `GET /api/v1/jobs/{job_id}`로 조회한다. 같은 `external_event_id`를 다시 보내면 동일한 `job_id`와 `duplicate=true`가 반환된다.

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
- 운영 Profile에서는 `ENABLE_LOCAL_EVENTS=false`로 로컬 우회 Endpoint를 숨긴다.

요구사항 충돌과 결정 근거는 [docs/REQUIREMENTS_REVIEW.md](docs/REQUIREMENTS_REVIEW.md)에 기록했다.
