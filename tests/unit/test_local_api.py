from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import get_ingestion_service
from app.common.config import Settings, get_settings
from app.common.domain import EventSource, JobStatus
from app.db.models import AiJob
from app.main import create_app
from app.services.ingestion import IncomingJob, IngestionResult


pytestmark = pytest.mark.asyncio


class FakeIngestionService:
    """DB 없이 API 접수와 조회 계약을 검증하는 대역이다."""

    def __init__(self, created: bool = True, job: AiJob | None = None) -> None:
        """생성 여부와 조회 결과를 테스트에서 지정한다."""
        self.job_id = uuid4()
        self.created = created
        self.job = job
        self.accepted: list[IncomingJob] = []

    async def accept(self, incoming: IncomingJob) -> IngestionResult:
        """접수 명령을 기록하고 고정 결과를 반환한다."""
        self.accepted.append(incoming)
        return IngestionResult(job_id=self.job_id, created=self.created)

    async def get_job(self, _: UUID) -> AiJob | None:
        """설정된 작업 또는 미존재 결과를 반환한다."""
        return self.job


def build_client(service: FakeIngestionService, enabled: bool = True) -> AsyncClient:
    """테스트 대역과 로컬 이벤트 Profile을 주입한 Client를 만든다."""
    application = create_app()

    def override_service() -> FakeIngestionService:
        """동일한 접수 대역을 요청 의존성으로 반환한다."""
        return service

    def override_settings() -> Settings:
        """테스트별 로컬 Endpoint 노출 설정을 반환한다."""
        return Settings(
            enable_local_events=enabled,
            slack_signing_secret="test-secret",
        )

    application.dependency_overrides[get_ingestion_service] = override_service
    application.dependency_overrides[get_settings] = override_settings
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://test")


async def test_live_health_is_available_without_dependencies() -> None:
    """외부 서비스 없이 Liveness가 정상 응답하는지 검증한다."""
    service = FakeIngestionService()
    async with build_client(service) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "x-request-id" in response.headers


async def test_local_event_returns_accepted_job() -> None:
    """정상 로컬 이벤트가 202와 신규 작업 ID를 반환하는지 검증한다."""
    service = FakeIngestionService(created=True)
    async with build_client(service) as client:
        response = await client.post(
            "/api/v1/events",
            json={"external_event_id": "local-1", "question": "처리 방법은?"},
        )

    assert response.status_code == 202
    assert response.json() == {"job_id": str(service.job_id), "duplicate": False}
    assert service.accepted[0].source == EventSource.LOCAL


async def test_local_duplicate_returns_same_job_as_duplicate() -> None:
    """중복 로컬 이벤트 응답 정책이 202와 duplicate 표시인지 검증한다."""
    service = FakeIngestionService(created=False)
    async with build_client(service) as client:
        response = await client.post(
            "/api/v1/events",
            json={"external_event_id": "local-1", "question": "처리 방법은?"},
        )

    assert response.status_code == 202
    assert response.json()["duplicate"] is True


async def test_local_event_is_hidden_when_profile_is_disabled() -> None:
    """운영 Profile에서 로컬 우회 Endpoint가 노출되지 않는지 검증한다."""
    service = FakeIngestionService()
    async with build_client(service, enabled=False) as client:
        response = await client.post(
            "/api/v1/events",
            json={"external_event_id": "local-1", "question": "처리 방법은?"},
        )

    assert response.status_code == 404
    assert service.accepted == []


async def test_local_event_rejects_blank_question() -> None:
    """공백 질문이 저장 계층에 도달하기 전에 거절되는지 검증한다."""
    service = FakeIngestionService()
    async with build_client(service) as client:
        response = await client.post(
            "/api/v1/events",
            json={"external_event_id": "local-1", "question": "   "},
        )

    assert response.status_code == 422
    assert service.accepted == []


async def test_get_job_returns_state_and_hides_question() -> None:
    """작업 조회가 상태를 반환하되 질문 원문은 노출하지 않는지 검증한다."""
    job_id = uuid4()
    job = AiJob(
        job_id=job_id,
        source=EventSource.LOCAL,
        external_event_id="local-1",
        question="민감할 수 있는 질문",
        status=JobStatus.RECEIVED,
        attempt_count=0,
        created_at=datetime.now(UTC),
    )
    service = FakeIngestionService(job=job)
    async with build_client(service) as client:
        response = await client.get(f"/api/v1/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "RECEIVED"
    assert "question" not in response.json()


async def test_get_unknown_job_returns_not_found() -> None:
    """존재하지 않는 작업이 안전한 404 오류를 반환하는지 검증한다."""
    service = FakeIngestionService(job=None)
    async with build_client(service) as client:
        response = await client.get(f"/api/v1/jobs/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "JOB_NOT_FOUND"
