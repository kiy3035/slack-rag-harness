import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.common.domain import EventSource, JobStatus, OutboxStatus
from app.db.models import AiJob, JobOutbox
from app.services.ingestion import IncomingJob, JobRepository


pytestmark = pytest.mark.integration


async def accept_once(
    session_factory: async_sessionmaker[AsyncSession],
    event_id: str,
) -> UUID:
    """독립 세션에서 동일 외부 이벤트를 한 번 접수한다."""
    async with session_factory() as session:
        result = await JobRepository(session).create_with_outbox(
            IncomingJob(
                source=EventSource.LOCAL,
                external_event_id=event_id,
                question="동시 요청 검증",
            )
        )
        return result.job_id


@pytest.mark.asyncio
async def test_concurrent_duplicate_creates_one_job_and_one_outbox(
    clean_database: AsyncEngine,
) -> None:
    """동시 중복 요청을 DB UNIQUE 제약이 한 작업과 Outbox로 축약하는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    job_ids = await asyncio.gather(
        *(accept_once(session_factory, "same-event") for _ in range(8))
    )

    async with session_factory() as session:
        job_count = await session.scalar(select(func.count()).select_from(AiJob))
        outbox_count = await session.scalar(select(func.count()).select_from(JobOutbox))

    assert len(set(job_ids)) == 1
    assert job_count == 1
    assert outbox_count == 1


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_job(clean_database: AsyncEngine) -> None:
    """Outbox INSERT 실패 시 같은 트랜잭션의 작업도 남지 않는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    conflict_outbox_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
            seed_job = AiJob(
                job_id=uuid4(),
                source=EventSource.LOCAL,
                external_event_id="seed-event",
                question="충돌 준비",
                status=JobStatus.RECEIVED,
                attempt_count=0,
            )
            session.add(seed_job)
            await session.flush()
            session.add(
                JobOutbox(
                    outbox_id=conflict_outbox_id,
                    job_id=seed_job.job_id,
                    status=OutboxStatus.READY,
                    attempt_count=0,
                )
            )

    def fixed_outbox_id() -> UUID:
        """PK 충돌을 재현할 고정 Outbox 식별자를 반환한다."""
        return conflict_outbox_id

    async with session_factory() as session:
        repository = JobRepository(session, outbox_id_factory=fixed_outbox_id)
        with pytest.raises(IntegrityError):
            await repository.create_with_outbox(
                IncomingJob(
                    source=EventSource.LOCAL,
                    external_event_id="must-rollback",
                    question="롤백 확인",
                )
            )

    async with session_factory() as session:
        rolled_back_job = await session.scalar(
            select(AiJob).where(AiJob.external_event_id == "must-rollback")
        )
        job_count = await session.scalar(select(func.count()).select_from(AiJob))
        outbox_count = await session.scalar(select(func.count()).select_from(JobOutbox))

    assert rolled_back_job is None
    assert job_count == 1
    assert outbox_count == 1
