import asyncio
from datetime import UTC, datetime, timedelta
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.common.config import Settings
from app.common.domain import EventSource, JobStatus, OutboxStatus
from app.db.models import AiJob, JobOutbox
from app.messaging.messages import JobMessage
from app.messaging.rabbitmq import RabbitBroker
from app.outbox.publisher import OutboxPublisher
from app.outbox.repository import OutboxRepository
from app.services.ingestion import IncomingJob, JobRepository
from app.worker.consumer import JobExecutionGate, RabbitJobConsumer


pytestmark = pytest.mark.integration


class RecordingHandler:
    """실제로 실행된 작업 메시지만 순서대로 기록하는 Handler 대역이다."""

    def __init__(self) -> None:
        """호출된 메시지를 보관할 빈 목록을 만든다."""
        self.messages: list[JobMessage] = []

    async def __call__(self, message: JobMessage) -> None:
        """Consumer가 실행을 허용한 메시지를 기록한다."""
        self.messages.append(message)


def build_isolated_settings() -> Settings:
    """다른 실행과 충돌하지 않는 RabbitMQ Queue 이름을 만든다."""
    rabbitmq_url = os.environ.get("TEST_RABBITMQ_URL")
    if rabbitmq_url is None:
        raise RuntimeError("TEST_RABBITMQ_URL이 필요합니다.")
    suffix = uuid4().hex[:12]
    return Settings(
        rabbitmq_url=rabbitmq_url,
        rabbitmq_job_exchange=f"test.jobs.{suffix}",
        rabbitmq_retry_exchange=f"test.jobs.retry.{suffix}",
        rabbitmq_dead_letter_exchange=f"test.jobs.dlx.{suffix}",
        rabbitmq_job_queue=f"test.jobs.{suffix}",
        rabbitmq_retry_queue=f"test.jobs.retry.{suffix}",
        rabbitmq_dead_letter_queue=f"test.jobs.dlq.{suffix}",
        rabbitmq_retry_delay_ms=60_000,
        outbox_lease_seconds=30,
        outbox_max_attempts=3,
    )


async def create_job(
    session_factory: async_sessionmaker[AsyncSession],
    event_id: str,
) -> tuple[UUID, UUID]:
    """통합 테스트용 작업과 READY Outbox 식별자를 생성한다."""
    async with session_factory() as session:
        result = await JobRepository(session).create_with_outbox(
            IncomingJob(
                source=EventSource.LOCAL,
                external_event_id=event_id,
                question="2단계 메시징 검증",
            )
        )
    async with session_factory() as session:
        outbox_id = await session.scalar(
            select(JobOutbox.outbox_id).where(JobOutbox.job_id == result.job_id)
        )
    if outbox_id is None:
        raise AssertionError("Outbox가 생성되지 않았습니다.")
    return result.job_id, outbox_id


@pytest.mark.asyncio
async def test_publisher_and_consumer_process_duplicate_job_once(
    clean_database: AsyncEngine,
) -> None:
    """실제 RabbitMQ 중복 메시지가 DB Gate를 지나 한 번만 실행되는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    job_id, outbox_id = await create_job(session_factory, "stage2-normal")
    settings = build_isolated_settings()
    broker = RabbitBroker(settings)
    await broker.connect()
    try:
        publisher = OutboxPublisher(OutboxRepository(session_factory), broker, settings)
        cycle = await publisher.process_once()
        duplicate_message = JobMessage(
            outbox_id=outbox_id,
            job_id=job_id,
            request_id="stage2-normal",
        )
        await broker.publish_job(duplicate_message)

        handler = RecordingHandler()
        consumer = RabbitJobConsumer(broker, JobExecutionGate(session_factory), handler)
        assert await consumer.consume_one()
        assert await consumer.consume_one()

        async with session_factory() as session:
            outbox = await session.get(JobOutbox, outbox_id)
            job = await session.get(AiJob, job_id)
        assert cycle.sent == 1
        assert outbox is not None and outbox.status == OutboxStatus.SENT
        assert job is not None and job.status == JobStatus.PROCESSING
        assert job.attempt_count == 1
        assert len(handler.messages) == 1
    finally:
        await broker.delete_topology()
        await broker.close()


@pytest.mark.asyncio
async def test_two_publishers_claim_each_outbox_at_most_once(
    clean_database: AsyncEngine,
) -> None:
    """두 Publisher가 동시에 선점해도 같은 Outbox를 공유하지 않는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    await create_job(session_factory, "stage2-claim-1")
    await create_job(session_factory, "stage2-claim-2")
    now = datetime.now(UTC)
    first_repository = OutboxRepository(session_factory)
    second_repository = OutboxRepository(session_factory)

    first, second = await asyncio.gather(
        first_repository.claim_ready(now=now, limit=2),
        second_repository.claim_ready(now=now, limit=2),
    )

    claimed_ids = [claim.outbox_id for claim in [*first, *second]]
    assert len(claimed_ids) == 2
    assert len(set(claimed_ids)) == 2


@pytest.mark.asyncio
async def test_stale_processing_is_recovered_and_bounded(
    clean_database: AsyncEngine,
) -> None:
    """오래된 Lease가 복구되고 횟수 소진 시 작업까지 실패하는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    job_id, outbox_id = await create_job(session_factory, "stage2-stale")
    now = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
    stale_time = now - timedelta(minutes=5)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(JobOutbox)
            .where(JobOutbox.outbox_id == outbox_id)
            .values(status=OutboxStatus.PROCESSING, locked_at=stale_time)
        )
    repository = OutboxRepository(session_factory)

    recovered, exhausted = await repository.recover_stale(
        now=now,
        lease_timeout=timedelta(seconds=30),
        max_attempts=3,
    )
    assert (recovered, exhausted) == (1, 0)

    async with session_factory() as session, session.begin():
        await session.execute(
            update(JobOutbox)
            .where(JobOutbox.outbox_id == outbox_id)
            .values(
                status=OutboxStatus.PROCESSING,
                attempt_count=2,
                locked_at=stale_time,
            )
        )
    recovered, exhausted = await repository.recover_stale(
        now=now,
        lease_timeout=timedelta(seconds=30),
        max_attempts=3,
    )

    async with session_factory() as session:
        outbox = await session.get(JobOutbox, outbox_id)
        job = await session.get(AiJob, job_id)
    assert (recovered, exhausted) == (0, 1)
    assert outbox is not None and outbox.status == OutboxStatus.FAIL
    assert job is not None and job.status == JobStatus.FAILED
    assert job.failure_code == "OUTBOX_LEASE_EXHAUSTED"


@pytest.mark.asyncio
async def test_publish_failures_wait_then_exhaust(
    clean_database: AsyncEngine,
) -> None:
    """발행 실패가 다음 시각 전에는 선점되지 않고 제한 횟수에서 종료되는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    job_id, outbox_id = await create_job(session_factory, "stage2-backoff")
    repository = OutboxRepository(session_factory)
    now = datetime(2026, 9, 20, 4, 0, tzinfo=UTC)
    first_claims = await repository.claim_ready(now=now, limit=1)
    assert len(first_claims) == 1

    first_failure = await repository.record_failure(
        first_claims[0],
        now=now,
        next_retry_at=now + timedelta(seconds=10),
        error="AMQP_CONNECTION_FAILED",
        max_attempts=2,
    )
    early_claims = await repository.claim_ready(
        now=now + timedelta(seconds=9),
        limit=1,
    )
    second_claims = await repository.claim_ready(
        now=now + timedelta(seconds=10),
        limit=1,
    )

    assert first_failure.updated and not first_failure.exhausted
    assert early_claims == []
    assert len(second_claims) == 1
    second_failure = await repository.record_failure(
        second_claims[0],
        now=now + timedelta(seconds=10),
        next_retry_at=now + timedelta(seconds=30),
        error="AMQP_CONNECTION_FAILED",
        max_attempts=2,
    )
    async with session_factory() as session:
        outbox = await session.get(JobOutbox, outbox_id)
        job = await session.get(AiJob, job_id)
    assert second_failure.updated and second_failure.exhausted
    assert outbox is not None and outbox.status == OutboxStatus.FAIL
    assert outbox.attempt_count == 2
    assert outbox.next_retry_at is None
    assert job is not None and job.status == JobStatus.FAILED
    assert job.failure_code == "OUTBOX_PUBLISH_EXHAUSTED"


@pytest.mark.asyncio
async def test_retry_and_dead_letter_queues_route_messages() -> None:
    """거절 메시지가 Retry Queue로 이동하고 명시적 실패가 DLQ에 저장되는지 검증한다."""
    settings = build_isolated_settings()
    broker = RabbitBroker(settings)
    await broker.connect()
    try:
        message = JobMessage(
            outbox_id=uuid4(),
            job_id=uuid4(),
            request_id="stage2-routing",
        )
        await broker.publish_job(message)
        delivery = await broker.topology.job_queue.get(timeout=2, fail=False)
        assert delivery is not None
        await delivery.reject(requeue=False)

        retry_delivery = await broker.topology.retry_queue.get(timeout=2, fail=False)
        assert retry_delivery is not None
        assert retry_delivery.body == message.model_dump_json().encode("utf-8")
        await retry_delivery.ack()

        await broker.publish_dead_letter(
            b'{"invalid":true}',
            message_id="invalid-message",
            correlation_id=None,
            error_code="MESSAGE_SCHEMA_INVALID",
        )
        dead_delivery = await broker.topology.dead_letter_queue.get(timeout=2, fail=False)
        assert dead_delivery is not None
        assert dead_delivery.headers["x-error-code"] == "MESSAGE_SCHEMA_INVALID"
        await dead_delivery.ack()
    finally:
        await broker.delete_topology()
        await broker.close()
