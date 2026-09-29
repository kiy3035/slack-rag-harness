from dataclasses import dataclass
from typing import Self

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator


class SlackClientConfigurationError(RuntimeError):
    """Slack 발신에 필요한 안전한 로컬 설정이 빠졌음을 나타낸다."""


class SlackPublishError(RuntimeError):
    """Slack API 실패를 재시도 가능 여부와 안전한 코드로 전달한다."""

    def __init__(
        self,
        error_code: str,
        *,
        transient: bool,
        retry_after_seconds: int | None = None,
    ) -> None:
        """원격 응답 원문 대신 제한된 복구 정보만 예외에 저장한다."""
        super().__init__(error_code)
        self.error_code = error_code
        self.transient = transient
        self.retry_after_seconds = retry_after_seconds


class SlackPostMessageResponse(BaseModel):
    """chat.postMessage의 성공 여부와 메시지 식별자만 검증한다."""

    model_config = ConfigDict(extra="allow")

    ok: bool
    ts: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def require_success_timestamp(self) -> Self:
        """성공 응답에 후속 추적 가능한 Slack Timestamp가 있는지 확인한다."""
        if self.ok and not self.ts:
            raise ValueError("Slack 성공 응답에 ts가 없습니다.")
        if not self.ok and not self.error:
            raise ValueError("Slack 실패 응답에 error가 없습니다.")
        return self


@dataclass(frozen=True, slots=True)
class SlackPostResult:
    """성공한 Slack Thread 답변의 원격 메시지 식별자를 보관한다."""

    message_ts: str


class SlackWebClient:
    """Bot Token을 Authorization Header로만 보내 Slack Thread에 답변한다."""

    _TRANSIENT_API_ERRORS = {
        "fatal_error",
        "internal_error",
        "ratelimited",
        "request_timeout",
        "service_unavailable",
    }

    def __init__(
        self,
        *,
        bot_token: str,
        base_url: str = "https://slack.com/api",
        timeout_seconds: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """비어 있지 않은 Bot Token과 교체 가능한 HTTP Client를 구성한다."""
        token = bot_token.strip()
        if not token:
            raise SlackClientConfigurationError("SLACK_BOT_TOKEN_MISSING")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            timeout=timeout_seconds,
        )
        self._authorization = f"Bearer {token}"

    async def post_thread_reply(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        text: str,
        client_message_id: str,
    ) -> SlackPostResult:
        """답변을 지정 Thread에 게시하고 Slack 메시지 Timestamp를 반환한다."""
        try:
            response = await self._client.post(
                "chat.postMessage",
                headers={"authorization": self._authorization},
                json={
                    "channel": channel_id,
                    "thread_ts": thread_ts,
                    "text": text,
                    "client_msg_id": client_message_id,
                    "unfurl_links": False,
                    "unfurl_media": False,
                },
            )
        except httpx.TimeoutException as error:
            raise SlackPublishError("SLACK_TIMEOUT", transient=True) from error
        except httpx.RequestError as error:
            raise SlackPublishError("SLACK_UNAVAILABLE", transient=True) from error

        if response.status_code == 429:
            raise SlackPublishError(
                "SLACK_RATE_LIMITED",
                transient=True,
                retry_after_seconds=self._parse_retry_after(response),
            )
        if response.status_code >= 500:
            raise SlackPublishError(
                f"SLACK_HTTP_{response.status_code}", transient=True
            )
        if response.status_code >= 400:
            raise SlackPublishError(
                f"SLACK_HTTP_{response.status_code}", transient=False
            )

        try:
            payload = SlackPostMessageResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise SlackPublishError("SLACK_RESPONSE_INVALID", transient=True) from error
        if not payload.ok:
            error_name = payload.error or "unknown_error"
            safe_name = "".join(
                character if character.isalnum() else "_"
                for character in error_name.upper()
            )[:60]
            raise SlackPublishError(
                f"SLACK_API_{safe_name}",
                transient=error_name in self._TRANSIENT_API_ERRORS,
            )
        return SlackPostResult(message_ts=payload.ts or "")

    def _parse_retry_after(self, response: httpx.Response) -> int | None:
        """429 응답의 Retry-After 초를 양수 정수일 때만 채택한다."""
        raw_value = response.headers.get("retry-after")
        if raw_value is None:
            return None
        try:
            value = int(raw_value)
        except ValueError:
            return None
        return value if value > 0 else None

    async def aclose(self) -> None:
        """내부에서 만든 HTTP 연결 풀만 안전하게 닫는다."""
        if self._owns_client:
            await self._client.aclose()
