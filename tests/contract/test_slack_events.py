import hashlib
import hmac
import json
from pathlib import Path
import time
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Response

from app.api.dependencies import get_ingestion_service
from app.common.config import Settings, get_settings
from app.common.domain import EventSource
from app.main import create_app
from app.services.ingestion import IncomingJob, IngestionResult


FIXTURE_DIRECTORY = Path(__file__).parents[1] / "fixtures"
SIGNING_SECRET = "fixture-signing-secret"
pytestmark = pytest.mark.asyncio


class FakeSlackIngestionService:
    """Slack 계약 테스트에서 저장 명령을 관찰하는 대역이다."""

    def __init__(self, created: bool = True) -> None:
        """신규 또는 중복 저장 결과를 선택할 수 있게 구성한다."""
        self.job_id = uuid4()
        self.created = created
        self.accepted: list[IncomingJob] = []

    async def accept(self, incoming: IncomingJob) -> IngestionResult:
        """Slack에서 변환된 접수 명령을 기록한다."""
        self.accepted.append(incoming)
        return IngestionResult(job_id=self.job_id, created=self.created)

    async def get_job(self, _: UUID) -> None:
        """이 계약 테스트에서 사용하지 않는 조회를 빈 결과로 처리한다."""
        return None


def load_fixture(name: str) -> dict[str, object]:
    """실제 Slack 형태를 모사한 JSON Fixture를 읽는다."""
    return json.loads((FIXTURE_DIRECTORY / name).read_text(encoding="utf-8"))


def sign_request(body: bytes, timestamp: str) -> str:
    """Fixture 원본 바이트에 Slack v0 테스트 서명을 계산한다."""
    base = b"v0:" + timestamp.encode("ascii") + b":" + body
    return "v0=" + hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        base,
        hashlib.sha256,
    ).hexdigest()


def override_settings() -> Settings:
    """Slack 계약 테스트용 안전한 로컬 설정을 반환한다."""
    return Settings(enable_local_events=True, slack_signing_secret=SIGNING_SECRET)


def build_client(service: FakeSlackIngestionService) -> AsyncClient:
    """Slack 저장 대역과 Signing Secret을 주입한 Client를 만든다."""
    application = create_app()

    def override_service() -> FakeSlackIngestionService:
        """동일한 저장 대역을 요청 의존성으로 반환한다."""
        return service

    application.dependency_overrides[get_ingestion_service] = override_service
    application.dependency_overrides[get_settings] = override_settings
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://test")


async def post_signed(
    client: AsyncClient,
    payload: dict[str, object],
    timestamp: str | None = None,
) -> Response:
    """JSON 직렬화 전 원본 바이트와 일치하는 서명으로 요청한다."""
    actual_timestamp = timestamp or str(int(time.time()))
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return await client.post(
        "/api/v1/slack/events",
        content=body,
        headers={
            "content-type": "application/json",
            "x-slack-request-timestamp": actual_timestamp,
            "x-slack-signature": sign_request(body, actual_timestamp),
        },
    )


async def test_url_verification_returns_challenge_after_signature_check() -> None:
    """서명된 URL 검증 요청이 challenge를 그대로 반환하는지 검증한다."""
    service = FakeSlackIngestionService()
    async with build_client(service) as client:
        response = await post_signed(client, load_fixture("slack_url_verification.json"))

    assert response.status_code == 200
    assert response.json() == {"challenge": "fixture-challenge"}
    assert service.accepted == []


async def test_invalid_signature_is_rejected() -> None:
    """본문과 일치하지 않는 Slack 서명이 401로 거절되는지 검증한다."""
    service = FakeSlackIngestionService()
    body = b'{"type":"url_verification","challenge":"fixture-challenge"}'
    async with build_client(service) as client:
        response = await client.post(
            "/api/v1/slack/events",
            content=body,
            headers={
                "content-type": "application/json",
                "x-slack-request-timestamp": str(int(time.time())),
                "x-slack-signature": "v0=invalid",
            },
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "SLACK_SIGNATURE_INVALID"


async def test_stale_timestamp_is_rejected() -> None:
    """허용 범위를 넘긴 Timestamp가 올바른 서명이어도 거절되는지 검증한다."""
    service = FakeSlackIngestionService()
    stale_timestamp = str(int(time.time()) - 301)
    async with build_client(service) as client:
        response = await post_signed(
            client,
            load_fixture("slack_url_verification.json"),
            stale_timestamp,
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "SLACK_TIMESTAMP_STALE"


async def test_bot_message_is_acknowledged_without_creating_job() -> None:
    """Bot 자신이 만든 이벤트를 ACK하되 작업은 만들지 않는지 검증한다."""
    service = FakeSlackIngestionService()
    payload = load_fixture("slack_app_mention.json")
    event = payload["event"]
    assert isinstance(event, dict)
    event["bot_id"] = "B_FIXTURE"
    event["subtype"] = "bot_message"
    async with build_client(service) as client:
        response = await post_signed(client, payload)

    assert response.status_code == 200
    assert response.json()["ignored"] is True
    assert service.accepted == []


async def test_app_mention_is_stored_with_thread_target_and_fast_ack() -> None:
    """app_mention이 회신 위치와 함께 접수되고 3초 안에 ACK되는지 검증한다."""
    service = FakeSlackIngestionService()
    started_at = time.perf_counter()
    async with build_client(service) as client:
        response = await post_signed(client, load_fixture("slack_app_mention.json"))
    duration_seconds = time.perf_counter() - started_at

    assert response.status_code == 200
    assert response.json()["duplicate"] is False
    assert duration_seconds < 3
    incoming = service.accepted[0]
    assert incoming.source == EventSource.SLACK
    assert incoming.question == "정산 배치 처리 방법은?"
    assert incoming.slack_channel_id == "C_FIXTURE"
    assert incoming.slack_message_ts == "1710000000.000100"
    assert incoming.slack_thread_ts == "1710000000.000100"


async def test_slack_retry_returns_duplicate_ack() -> None:
    """이미 저장된 Slack event_id가 성공 ACK와 중복 표시를 받는지 검증한다."""
    service = FakeSlackIngestionService(created=False)
    async with build_client(service) as client:
        response = await post_signed(client, load_fixture("slack_app_mention.json"))

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "ignored": False,
        "duplicate": True,
        "job_id": str(service.job_id),
    }


async def test_thread_mention_keeps_parent_thread_timestamp() -> None:
    """Thread 안의 멘션은 답글 자체가 아니라 원래 부모 Thread를 회신 대상으로 보관한다."""
    service = FakeSlackIngestionService()
    payload = load_fixture("slack_app_mention.json")
    event = payload["event"]
    assert isinstance(event, dict)
    event["thread_ts"] = "1709999999.000001"
    async with build_client(service) as client:
        response = await post_signed(client, payload)

    assert response.status_code == 200
    assert service.accepted[0].slack_thread_ts == "1709999999.000001"


async def test_any_message_subtype_is_ignored() -> None:
    """Slack이 붙인 메시지 subtype은 Bot 반복 유입 가능성이 있어 작업을 만들지 않는다."""
    service = FakeSlackIngestionService()
    payload = load_fixture("slack_app_mention.json")
    event = payload["event"]
    assert isinstance(event, dict)
    event["subtype"] = "message_changed"
    async with build_client(service) as client:
        response = await post_signed(client, payload)

    assert response.status_code == 200
    assert response.json()["ignored"] is True
    assert service.accepted == []


async def test_empty_question_after_bot_mention_is_ignored() -> None:
    """Bot 멘션만 있는 이벤트를 빈 AI 작업으로 저장하지 않고 성공 ACK한다."""
    service = FakeSlackIngestionService()
    payload = load_fixture("slack_app_mention.json")
    event = payload["event"]
    assert isinstance(event, dict)
    event["text"] = "  <@B_FIXTURE>  "
    async with build_client(service) as client:
        response = await post_signed(client, payload)

    assert response.status_code == 200
    assert response.json()["ignored"] is True
    assert service.accepted == []
