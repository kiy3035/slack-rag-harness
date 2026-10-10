from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.common.config import Settings, get_settings
from app.common.domain import EventSource, JobStatus, ReviewStatus
from app.db.models import AiJob, ReviewQueue
from app.db.session import get_session
from app.main import create_app


pytestmark = pytest.mark.integration


def observability_settings() -> Settings:
    """통합 테스트에서 로컬 관리 화면만 명시적으로 활성화한다."""
    return Settings(enable_admin_observability=True)


def build_observability_app(engine: AsyncEngine) -> FastAPI:
    """격리된 테스트 DB Session을 사용하는 관측 API 애플리케이션을 만든다."""
    application = create_app()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        """각 요청에 테스트 DB 비동기 Session을 제공한다."""
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_settings] = observability_settings
    return application


async def seed_observability_rows(engine: AsyncEngine) -> tuple[str, str]:
    """민감 본문이 관리 화면에 노출되지 않는 작업과 검토 행을 만든다."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    job_id = uuid4()
    review_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            AiJob(
                job_id=job_id,
                source=EventSource.LOCAL,
                external_event_id=f"stage8-{job_id}",
                question="절대 노출하면 안 되는 질문",
                status=JobStatus.REVIEW_REQUIRED,
                attempt_count=2,
                workflow_revision=0,
                created_at=datetime(2026, 10, 10, 11, 38, 17, tzinfo=UTC),
            )
        )
        session.add(
            ReviewQueue(
                review_id=review_id,
                job_id=job_id,
                workflow_revision=0,
                reason_code="INSUFFICIENT_EVIDENCE",
                draft_answer="절대 노출하면 안 되는 초안",
                draft_citations=[],
                allowed_citations=[],
                status=ReviewStatus.WAITING,
                created_at=datetime(2026, 10, 10, 11, 38, 25, tzinfo=UTC),
            )
        )
    return str(job_id), str(review_id)


@pytest.mark.asyncio
async def test_metrics_exposes_api_latency_and_database_backlog(
    clean_database: AsyncEngine,
) -> None:
    """Prometheus Endpoint가 API 요청과 작업·검토 상태 Gauge를 함께 노출하는지 검증한다."""
    await seed_observability_rows(clean_database)
    transport = ASGITransport(app=build_observability_app(clean_database))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health/live")).status_code == 200
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert 'rag_harness_jobs{status="REVIEW_REQUIRED"} 1.0' in response.text
    assert 'rag_harness_review_queue{status="WAITING"} 1.0' in response.text
    assert 'rag_harness_api_requests_total{method="GET",route="/health/live",status_code="200"}' in response.text
    assert "rag_harness_api_request_duration_seconds_bucket" in response.text


@pytest.mark.asyncio
async def test_admin_page_shows_safe_operational_fields_only(
    clean_database: AsyncEngine,
) -> None:
    """관리 화면이 상태 식별자는 표시하되 질문과 답변 본문을 숨기는지 검증한다."""
    job_id, review_id = await seed_observability_rows(clean_database)
    transport = ASGITransport(app=build_observability_app(clean_database))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert job_id in response.text
    assert review_id in response.text
    assert "REVIEW_REQUIRED" in response.text
    assert "INSUFFICIENT_EVIDENCE" in response.text
    assert "한국 표준시(KST, UTC+9)" in response.text
    assert "created_at (KST)" in response.text
    assert "2026-10-10 20:38:17 KST" in response.text
    assert "2026-10-10 20:38:25 KST" in response.text
    assert "2026-10-10T11:38" not in response.text
    assert "절대 노출하면 안 되는 질문" not in response.text
    assert "절대 노출하면 안 되는 초안" not in response.text


@pytest.mark.asyncio
async def test_admin_page_is_hidden_when_disabled(clean_database: AsyncEngine) -> None:
    """운영자가 관리 화면을 활성화하지 않으면 존재 자체를 404로 숨기는지 검증한다."""
    application = build_observability_app(clean_database)

    def disabled_settings() -> Settings:
        """관리 화면이 비활성화된 안전 기본 설정을 반환한다."""
        return Settings(enable_admin_observability=False)

    application.dependency_overrides[get_settings] = disabled_settings
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin")

    assert response.status_code == 404
