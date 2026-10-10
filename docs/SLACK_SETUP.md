# 실제 Slack 연결 가이드

이 문서는 무료 테스트 Workspace에서 `app_mention`을 로컬 FastAPI로 전달하고, 검증된 답변을 같은 Thread에서 확인하는 7단계 절차다. Bot Token과 Signing Secret은 사용자가 로컬 `.env`에 직접 입력하며 저장소·문서·로그에 복사하지 않는다.

## 1. 구현된 계약

- 수신: `POST /api/v1/slack/events`
- 이벤트: `app_mention`
- 요청 인증: 원본 Body, `X-Slack-Request-Timestamp`, `X-Slack-Signature`
- 중복 방지: `event_id` DB UNIQUE 제약
- 빠른 응답: 작업과 Outbox 저장 뒤 AI 처리를 기다리지 않고 ACK
- 발신: `POST https://slack.com/api/chat.postMessage`
- 회신 위치: 원본 `channel`, `event.thread_ts` 또는 최상위 `event.ts`
- 권한: `app_mentions:read`, `chat:write`

## 2. Slack App 준비

1. 무료 테스트 Workspace를 준비한다.
2. Slack API의 **Create New App → From scratch**에서 App을 만든다.
3. **OAuth & Permissions → Bot Token Scopes**에 `app_mentions:read`, `chat:write`를 추가한다.
4. **Install to Workspace**를 실행하고 `xoxb-`로 시작하는 Bot Token을 확인한다.
5. **Basic Information → App Credentials**에서 Signing Secret을 확인한다.
6. 테스트 채널에서 App을 초대한다.

다른 공개 채널에 초대 없이 쓰기 위한 `chat:write.public`은 이 테스트에 필요하지 않다. Bot은 초대된 채널에서만 검증한다.

## 3. 로컬 환경변수

Git에서 제외된 프로젝트 루트의 로컬 `.env`에 아래 값을 설정한다.

```dotenv
SLACK_REPLY_ENABLED=true
SLACK_BOT_TOKEN=xoxb-로컬에서만-입력
SLACK_SIGNING_SECRET=로컬에서만-입력
```

Secret 값을 채팅, 이슈, PR, 캡처, 로그에 넣지 않는다. `SLACK_REPLY_ENABLED=false`이면 수신 Fixture와 로컬 Workflow는 계속 동작하지만 Slack Web API 발신은 실행하지 않는다.

## 4. 로컬 서비스와 무료 Tunnel

Ollama 모델과 문서 적재를 준비한 뒤 서비스를 실행한다.

```powershell
ollama pull nomic-embed-text
ollama pull qwen3:1.7b
docker compose run --rm api python -m app.retrieval.main ingest knowledge/manuals
docker compose up --build -d postgres rabbitmq api outbox-publisher worker
Invoke-RestMethod http://localhost:8000/health/ready
```

개발·시연 용도의 무료 TryCloudflare Quick Tunnel을 별도 터미널에서 실행한다.

```powershell
cloudflared tunnel --url http://localhost:8000
```

출력된 임시 `https://...trycloudflare.com` 주소를 기록한다. Quick Tunnel은 테스트 전용이고 주소·가용성 보장이 없으며, 로컬 `.cloudflared`에 `config.yaml`이 있으면 동작하지 않을 수 있다.

## 5. Event Subscription

1. Slack App의 **Event Subscriptions**를 활성화한다.
2. Request URL에 `https://발급주소.trycloudflare.com/api/v1/slack/events`를 입력한다.
3. Slack이 보내는 서명된 `url_verification`이 성공해 **Verified**인지 확인한다.
4. **Subscribe to bot events**에 `app_mention`을 추가한다.
5. 변경된 Scope가 있으면 App을 Workspace에 다시 설치한다.

Tunnel 주소가 바뀌면 Request URL도 갱신해야 한다. 공개 URL은 Slack 이벤트 수신에만 사용하고 로컬 이벤트·관리자 복구 Endpoint는 외부 운영 환경에서 비활성화한다.

## 6. 실제 화면 E2E

1. Slack PC 앱에서 Bot이 초대된 테스트 채널을 연다.
2. `@Bot 정산 배치 처리 방법은?`처럼 가상 매뉴얼에 있는 질문을 보낸다.
3. 원본 메시지에 Thread 답변이 생성되는지 확인한다.
4. 채널 최상위에 새 메시지가 생기지 않았는지 확인한다.
5. 같은 이벤트 Fixture를 다시 전송해 `ai_job`과 `slack_reply_outbox`가 각각 한 건인지 확인한다.
6. Bot 답변이 새 작업을 만들지 않는지 확인한다.

검증용 SQL 예시:

```sql
SELECT job_id, status, slack_channel_id, slack_thread_ts
FROM ai_job
WHERE source = 'SLACK'
ORDER BY created_at DESC
LIMIT 5;

SELECT job_id, status, attempt_count, last_error, slack_message_ts, sent_at
FROM slack_reply_outbox
ORDER BY created_at DESC
LIMIT 5;
```

화면 캡처에는 Workspace 이름, 사용자 이름, Token, Signing Secret, 실제 회사 질문이 보이지 않게 한다.

## 7. 실패와 재시도

- HTTP 429: `Retry-After` 초와 지수 Backoff 중 큰 값을 사용한다.
- Timeout, 네트워크 오류, HTTP 5xx, Slack 내부 오류: 최대 `SLACK_REPLY_MAX_ATTEMPTS`까지 재시도한다.
- 잘못된 채널, 권한 오류 같은 영구 오류: 즉시 `FAIL`로 전환한다.
- 재시도 시각 전에는 Outbox를 다시 선점하지 않는다.
- `PROCESSING` 상태에서 Worker가 종료되면 `SLACK_REPLY_LEASE_SECONDS` 뒤에 다시 선점한다.
- Slack 성공 직후 DB 완료 기록 전에 Worker가 종료되면 중복 가능성이 남는다. 같은 `reply_id`를 `client_msg_id`로 전송하며 운영 확인은 DB와 Slack Thread를 함께 본다.

로그에는 `job_id`, `reply_id`, 시도 횟수, 안전한 오류 코드만 남고 질문·답변·Token·원격 오류 원문은 기록하지 않는다.

## 8. 공식 문서

- Slack `chat.postMessage`: https://api.slack.com/methods/chat.postMessage
- Slack Web API Rate Limits: https://docs.slack.dev/apis/web-api/rate-limits/
- TryCloudflare Quick Tunnels: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/
