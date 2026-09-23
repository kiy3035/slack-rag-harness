from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
import os
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.dependencies import get_manual_recovery_service
from app.common.config import Settings, get_settings
from app.common.domain import EventSource, JobStatus, OutboxStatus, ReviewStatus
from app.db.models import AiJob, JobOutbox, JobRecoveryRequest, ReviewQueue
from app.main import create_app
from app.messaging.rabbitmq import RabbitBroker
from app.outbox.publisher import OutboxPublisher
from app.outbox.repository import OutboxRepository
from app.recovery.errors import ErrorClassifier, RetryPolicy
from app.recovery.repository import JobRecoveryRepository
from app.recovery.scheduler import RecoveryScheduler
from app.recovery.service import ManualRecoveryService
from app.services.ingestion import IncomingJob, JobRepository
from app.worker.consumer import JobExecutionGate, RabbitJobConsumer
from app.workflow.model import WorkflowModelResponseError, WorkflowModelTimeoutError


pytestmark = pytest.mark.integration
FIXED_NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


class UnavailableOllamaHandler:
    """로컬 Ollama 중단을 Worker Handler 경계에서 결정적으로 재현한다."""

    async def __call__(self, _: object) -> None:
        """모든 작업에서 생성 모델 timeout을 발생시킨다."""
        raise WorkflowModelTimeoutError("테스트 Ollama timeout")


async def create_job(
    session_factory: async_sessionmaker[AsyncSession],
    event_id: str,
) -> tuple[UUID, UUID]:
    """복구 통합 테스트용 작업과 고유 Outbox 식별자를 생성한다."""
    async with session_factory() as session:
        result = await JobRepository(session).create_with_outbox(
            IncomingJob(
                source=EventSource.LOCAL,
                external_event_id=event_id,
                question="장애 복구 테스트 질문",
            )
        )
    async with session_factory() as session:
        outbox_id = await session.scalar(
            select(JobOutbox.outbox_id).where(JobOutbox.job_id == result.job_id)
        )
    if outbox_id is None:
        raise AssertionError("복구 테스트 Outbox가 생성되지 않았습니다.")
    return result.job_id, outbox_id


def isolated_settings() -> Settings:
    """실제 다른 Queue와 충돌하지 않는 6단계 RabbitMQ 설정을 만든다."""
    rabbitmq_url = os.environ.get("TEST_RABBITMQ_URL")
    if rabbitmq_url is None:
        raise RuntimeError("TEST_RABBITMQ_URL이 필요합니다.")
    suffix = uuid4().hex[:12]
    return Settings(
        rabbitmq_url=rabbitmq_url,
        rabbitmq_job_exchange=f"test.stage6.jobs.{suffix}",
        rabbitmq_retry_exchange=f"test.stage6.retry.{suffix}",
        rabbitmq_dead_letter_exchange=f"test.stage6.dlx.{suffix}",
        rabbitmq_job_queue=f"test.stage6.jobs.{suffix}",
        rabbitmq_retry_queue=f"test.stage6.retry.{suffix}",
        rabbitmq_dead_letter_queue=f"test.stage6.dlq.{suffix}",
        worker_processing_lease_seconds=10,
        worker_max_attempts=3,
        worker_retry_base_seconds=5,
        worker_retry_max_seconds=30,
    )


def fixed_clock() -> datetime:
    """복구 Scheduler 검증에 사용할 고정 UTC 시각을 반환한다."""
    return FIXED_NOW


@pytest.mark.asyncio
async def test_transient_failure_waits_for_backoff_then_reopens_outbox(
    clean_database: AsyncEngine,
) -> None:
    """Ollama 일시 오류가 즉시 반복되지 않고 Backoff 뒤 기존 Outbox를 다시 여는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    job_id, outbox_id = await create_job(session_factory, "stage6-transient")
    repository = JobRecoveryRepository(session_factory)
    policy = RetryPolicy(max_attempts=3, base_seconds=5, max_seconds=30)
    assert await repository.claim(job_id, now=FIXED_NOW)

    transition = await repository.record_failure(
        job_id,
        decision=ErrorClassifier().classify(
            WorkflowModelTimeoutError("테스트 timeout")
        ),
        policy=policy,
        now=FIXED_NOW,
    )

    assert transition.status == JobStatus.RETRY_WAIT
    assert transition.next_retry_at == FIXED_NOW + timedelta(seconds=5)
    assert await repository.release_due_retries(
        now=FIXED_NOW + timedelta(seconds=4), limit=10
    ) == 0
    assert await repository.release_due_retries(
        now=FIXED_NOW + timedelta(seconds=5), limit=10
    ) == 1

    async with session_factory() as session:
        job = await session.get(AiJob, job_id)
        outbox = await session.get(JobOutbox, outbox_id)
    assert job is not None and job.status == JobStatus.RECEIVED
    assert job.attempt_count == 1
    assert outbox is not None and outbox.status == OutboxStatus.READY
    assert outbox.attempt_count == 0


@pytest.mark.asyncio
async def test_consumer_classifies_ollama_outage_and_acks_after_retry_state_saved(
    clean_database: AsyncEngine,
) -> None:
    """Ollama 중단 메시지를 DB RETRY_WAIT 저장 뒤 ACK해 유실과 즉시 반복을 막는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    job_id, _ = await create_job(session_factory, "stage6-ollama-down")
    settings = isolated_settings()
    broker = RabbitBroker(settings)
    await broker.connect()
    try:
        publish_result = await OutboxPublisher(
            OutboxRepository(session_factory), broker, settings
        ).process_once()
        consumer = RabbitJobConsumer(
            broker,
            JobExecutionGate(
                session_factory,
                RetryPolicy(max_attempts=3, base_seconds=5, max_seconds=30),
            ),
            UnavailableOllamaHandler(),
        )
        assert await consumer.consume_one(timeout=2)

        async with session_factory() as session:
            job = await session.get(AiJob, job_id)
        duplicate = await broker.topology.job_queue.get(timeout=0.2, fail=False)
        assert publish_result.sent == 1
        assert job is not None and job.status == JobStatus.RETRY_WAIT
        assert job.failure_code == "OLLAMA_GENERATION_TIMEOUT"
        assert job.next_retry_at is not None
        assert duplicate is None
    finally:
        await broker.delete_topology()
        await broker.close()


@pytest.mark.asyncio
async def test_invalid_model_output_moves_job_to_human_review(
    clean_database: AsyncEngine,
) -> None:
    """재시도해도 안전하지 않은 모델 계약 오류가 검토 항목 한 건으로 남는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    job_id, _ = await create_job(session_factory, "stage6-review")
    repository = JobRecoveryRepository(session_factory)
    assert await repository.claim(job_id, now=FIXED_NOW)

    transition = await repository.record_failure(
        job_id,
        decision=ErrorClassifier().classify(
            WorkflowModelResponseError("잘못된 JSON")
        ),
        policy=RetryPolicy(max_attempts=3, base_seconds=5, max_seconds=30),
        now=FIXED_NOW,
    )

    async with session_factory() as session:
        job = await session.get(AiJob, job_id)
        review = await session.scalar(
            select(ReviewQueue).where(ReviewQueue.job_id == job_id)
        )
    assert transition.status == JobStatus.REVIEW_REQUIRED
    assert job is not None and job.status == JobStatus.REVIEW_REQUIRED
    assert review is not None and review.status == ReviewStatus.WAITING
    assert review.reason_code == "MODEL_OUTPUT_INVALID"


@pytest.mark.asyncio
async def test_stale_worker_exhaustion_is_linked_to_database_and_dlq(
    clean_database: AsyncEngine,
) -> None:
    """강제 종료가 반복된 작업이 DB DEAD_LETTER와 실제 DLQ 발행 완료로 함께 남는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    job_id, outbox_id = await create_job(session_factory, "stage6-stale")
    async with session_factory() as session, session.begin():
        await session.execute(
            update(AiJob)
            .where(AiJob.job_id == job_id)
            .values(
                status=JobStatus.PROCESSING,
                attempt_count=3,
                locked_at=FIXED_NOW - timedelta(minutes=5),
            )
        )
        await session.execute(
            update(JobOutbox)
            .where(JobOutbox.outbox_id == outbox_id)
            .values(status=OutboxStatus.SENT, sent_at=FIXED_NOW)
        )

    settings = isolated_settings()
    broker = RabbitBroker(settings)
    await broker.connect()
    try:
        result = await RecoveryScheduler(
            JobRecoveryRepository(session_factory),
            broker,
            settings,
            clock=fixed_clock,
        ).process_once()
        delivery = await broker.topology.dead_letter_queue.get(timeout=2, fail=False)
        assert delivery is not None
        assert delivery.headers["x-error-code"] == "WORKER_LEASE_EXHAUSTED"
        await delivery.ack()

        async with session_factory() as session:
            job = await session.get(AiJob, job_id)
        assert result.stale_exhausted == 1
        assert result.dlq_published == 1
        assert job is not None and job.status == JobStatus.DEAD_LETTER
        assert job.dlq_published_at == FIXED_NOW
    finally:
        await broker.delete_topology()
        await broker.close()


@pytest.mark.asyncio
async def test_admin_retry_endpoint_is_idempotent_and_starts_new_revision(
    clean_database: AsyncEngine,
) -> None:
    """같은 관리자 키의 재처리 요청이 Workflow 세대를 한 번만 올리는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    job_id, outbox_id = await create_job(session_factory, "stage6-manual")
    async with session_factory() as session, session.begin():
        await session.execute(
            update(AiJob)
            .where(AiJob.job_id == job_id)
            .values(
                status=JobStatus.DEAD_LETTER,
                attempt_count=3,
                failure_code="WORKER_RETRY_EXHAUSTED",
                completed_at=FIXED_NOW,
            )
        )
        await session.execute(
            update(JobOutbox)
            .where(JobOutbox.outbox_id == outbox_id)
            .values(status=OutboxStatus.SENT, sent_at=FIXED_NOW)
        )

    async def recovery_override() -> AsyncIterator[ManualRecoveryService]:
        """HTTP 요청마다 테스트 DB Session을 사용하는 재처리 서비스를 제공한다."""
        async with session_factory() as session:
            yield ManualRecoveryService(session)

    def settings_override() -> Settings:
        """통합 테스트에서만 로컬 관리자 재처리 Endpoint를 활성화한다."""
        return Settings(enable_admin_recovery=True)

    application = create_app()
    application.dependency_overrides[get_manual_recovery_service] = recovery_override
    application.dependency_overrides[get_settings] = settings_override
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        first = await client.post(
            f"/api/v1/admin/jobs/{job_id}/retry",
            json={"idempotency_key": "operator-request-1", "reason": "장애 조치 완료"},
        )
        duplicate = await client.post(
            f"/api/v1/admin/jobs/{job_id}/retry",
            json={"idempotency_key": "operator-request-1", "reason": "장애 조치 완료"},
        )

    assert first.status_code == 200
    assert first.json()["idempotent"] is False
    assert first.json()["workflow_revision"] == 1
    assert duplicate.status_code == 200
    assert duplicate.json()["idempotent"] is True
    assert duplicate.json()["workflow_revision"] == 1

    async with session_factory() as session:
        job = await session.get(AiJob, job_id)
        outbox = await session.get(JobOutbox, outbox_id)
        request_count = await session.scalar(
            select(func.count()).select_from(JobRecoveryRequest)
        )
    assert job is not None and job.status == JobStatus.RECEIVED
    assert job.attempt_count == 0
    assert outbox is not None and outbox.status == OutboxStatus.READY
    assert request_count == 1
