import asyncio
from collections.abc import AsyncIterator
import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.common.config import Settings, get_settings
from app.db.models import AiJob, JobOutbox
from app.db.session import get_session
from app.main import create_app


pytestmark = pytest.mark.integration
SIGNING_SECRET = "integration-signing-secret"


def integration_settings() -> Settings:
    """통합 테스트용 로컬 Event와 Slack Secret 설정을 반환한다."""
    return Settings(enable_local_events=True, slack_signing_secret=SIGNING_SECRET)


def sign_slack_request(body: bytes, timestamp: str) -> str:
    """통합 테스트의 원본 Slack 본문에 v0 서명을 계산한다."""
    base = b"v0:" + timestamp.encode("ascii") + b":" + body
    return "v0=" + hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        base,
        hashlib.sha256,
    ).hexdigest()


def build_test_app(clean_database: AsyncEngine) -> FastAPI:
    """실제 테스트 DB 세션을 사용하는 FastAPI 인스턴스를 구성한다."""
    application = create_app()
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        """각 HTTP 요청에 독립적인 실제 PostgreSQL 세션을 제공한다."""
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_settings] = integration_settings
    return application


async def post_local_event(client: AsyncClient) -> Response:
    """동일한 외부 ID를 가진 로컬 이벤트를 HTTP로 전송한다."""
    return await client.post(
        "/api/v1/events",
        json={"external_event_id": "concurrent-http-event", "question": "동시 요청"},
    )


@pytest.mark.asyncio
async def test_concurrent_http_events_are_idempotent(clean_database: AsyncEngine) -> None:
    """동시 HTTP 요청이 모두 같은 작업 ID를 받고 한 쌍만 저장되는지 검증한다."""
    application = build_test_app(clean_database)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(*(post_local_event(client) for _ in range(8)))

    assert all(response.status_code == 202 for response in responses)
    job_ids = {response.json()["job_id"] for response in responses}
    assert len(job_ids) == 1
    assert sum(not response.json()["duplicate"] for response in responses) == 1

    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    async with session_factory() as session:
        job_count = await session.scalar(select(func.count()).select_from(AiJob))
        outbox_count = await session.scalar(select(func.count()).select_from(JobOutbox))
    assert job_count == 1
    assert outbox_count == 1


@pytest.mark.asyncio
async def test_slack_event_ack_with_real_database_is_under_three_seconds(
    clean_database: AsyncEngine,
) -> None:
    """실제 DB 저장을 포함한 Slack 수신 경로가 3초 이내 ACK하는지 검증한다."""
    payload = {
        "type": "event_callback",
        "event_id": "Ev_INTEGRATION",
        "event": {
            "type": "app_mention",
            "text": "<@B_FIXTURE> 질문",
            "channel": "C_FIXTURE",
            "ts": "1710000000.000100",
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    application = build_test_app(clean_database)
    transport = ASGITransport(app=application)

    started_at = time.perf_counter()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/slack/events",
            content=body,
            headers={
                "content-type": "application/json",
                "x-slack-request-timestamp": timestamp,
                "x-slack-signature": sign_slack_request(body, timestamp),
            },
        )
    duration_seconds = time.perf_counter() - started_at

    assert response.status_code == 200
    assert duration_seconds < 3
    assert response.json()["job_id"] is not None


@pytest.mark.asyncio
async def test_job_can_be_read_after_acceptance(clean_database: AsyncEngine) -> None:
    """접수된 작업이 조회 Endpoint에서 RECEIVED 상태로 보이는지 검증한다."""
    application = build_test_app(clean_database)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/api/v1/events",
            json={"external_event_id": "read-event", "question": "상태 확인"},
        )
        response = await client.get(f"/api/v1/jobs/{accepted.json()['job_id']}")

    assert response.status_code == 200
    assert response.json()["status"] == "RECEIVED"
