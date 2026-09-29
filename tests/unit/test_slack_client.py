import json

import httpx
import pytest

from app.integrations.slack.client import SlackPublishError, SlackWebClient


pytestmark = pytest.mark.asyncio


async def test_post_thread_reply_sends_required_slack_contract() -> None:
    """Bot Token과 원본 Thread 대상이 chat.postMessage 계약대로 전달되는지 검증한다."""
    captured: dict[str, object] = {}

    def handle_request(request: httpx.Request) -> httpx.Response:
        """Slack 성공 응답을 돌려주며 외부로 나갈 요청 계약을 기록한다."""
        captured["authorization"] = request.headers.get("authorization")
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"ok": True, "channel": "C_TEST", "ts": "1710000001.000200"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle_request),
        base_url="https://slack.test/api",
    ) as http_client:
        client = SlackWebClient(bot_token="xoxb-safe-test", http_client=http_client)
        result = await client.post_thread_reply(
            channel_id="C_TEST",
            thread_ts="1710000000.000100",
            text="검증된 답변",
            client_message_id="reply-id",
        )

    assert result.message_ts == "1710000001.000200"
    assert captured["authorization"] == "Bearer xoxb-safe-test"
    assert captured["path"] == "/api/chat.postMessage"
    assert captured["payload"] == {
        "channel": "C_TEST",
        "thread_ts": "1710000000.000100",
        "text": "검증된 답변",
        "client_msg_id": "reply-id",
        "unfurl_links": False,
        "unfurl_media": False,
    }


async def test_rate_limit_uses_retry_after_without_exposing_response() -> None:
    """Slack 429가 Retry-After를 가진 일시 오류로 변환되는지 검증한다."""
    def handle_request(_: httpx.Request) -> httpx.Response:
        """고정 Retry-After가 포함된 Slack 제한 응답을 모사한다."""
        return httpx.Response(429, headers={"Retry-After": "37"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle_request),
        base_url="https://slack.test/api",
    ) as http_client:
        client = SlackWebClient(bot_token="xoxb-safe-test", http_client=http_client)
        with pytest.raises(SlackPublishError) as captured:
            await client.post_thread_reply(
                channel_id="C_TEST",
                thread_ts="1710000000.000100",
                text="답변",
                client_message_id="reply-id",
            )

    assert captured.value.error_code == "SLACK_RATE_LIMITED"
    assert captured.value.transient is True
    assert captured.value.retry_after_seconds == 37


async def test_slack_api_contract_error_is_permanent() -> None:
    """잘못된 채널 같은 영구 Slack API 오류를 무의미하게 재시도하지 않는지 검증한다."""
    def handle_request(_: httpx.Request) -> httpx.Response:
        """채널 권한 또는 식별자 오류에 해당하는 Slack 응답을 모사한다."""
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle_request),
        base_url="https://slack.test/api",
    ) as http_client:
        client = SlackWebClient(bot_token="xoxb-safe-test", http_client=http_client)
        with pytest.raises(SlackPublishError) as captured:
            await client.post_thread_reply(
                channel_id="C_UNKNOWN",
                thread_ts="1710000000.000100",
                text="답변",
                client_message_id="reply-id",
            )

    assert captured.value.error_code == "SLACK_API_CHANNEL_NOT_FOUND"
    assert captured.value.transient is False
