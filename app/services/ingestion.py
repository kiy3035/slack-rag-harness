from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.domain import EventSource, JobStatus, OutboxStatus
from app.db.models import AiJob, JobOutbox


@dataclass(frozen=True, slots=True)
class IncomingJob:
    """검증된 외부 이벤트를 저장 계층에 전달하는 명령이다."""

    source: EventSource
    external_event_id: str
    question: str
    slack_channel_id: str | None = None
    slack_message_ts: str | None = None
    slack_thread_ts: str | None = None


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """신규 생성 여부와 재사용된 작업 ID를 함께 반환한다."""

    job_id: UUID
    created: bool


class JobRepository:
    """작업과 Outbox를 같은 PostgreSQL 트랜잭션으로 다룬다."""

    def __init__(
        self,
        session: AsyncSession,
        job_id_factory: Callable[[], UUID] = uuid4,
        outbox_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        """세션과 교체 가능한 식별자 생성기를 주입한다."""
        self._session = session
        self._job_id_factory = job_id_factory
        self._outbox_id_factory = outbox_id_factory

    async def create_with_outbox(self, incoming: IncomingJob) -> IngestionResult:
        """중복 키를 원자적으로 판정하며 작업과 Outbox를 함께 저장한다."""
        candidate_job_id = self._job_id_factory()
        statement = (
            insert(AiJob)
            .values(
                job_id=candidate_job_id,
                source=incoming.source,
                external_event_id=incoming.external_event_id,
                question=incoming.question,
                slack_channel_id=incoming.slack_channel_id,
                slack_message_ts=incoming.slack_message_ts,
                slack_thread_ts=incoming.slack_thread_ts,
                status=JobStatus.RECEIVED,
                attempt_count=0,
            )
            .on_conflict_do_nothing(index_elements=["source", "external_event_id"])
            .returning(AiJob.job_id)
        )

        async with self._session.begin():
            inserted_job_id = (await self._session.execute(statement)).scalar_one_or_none()
            if inserted_job_id is None:
                existing_job_id = (
                    await self._session.execute(
                        select(AiJob.job_id).where(
                            AiJob.source == incoming.source,
                            AiJob.external_event_id == incoming.external_event_id,
                        )
                    )
                ).scalar_one()
                return IngestionResult(job_id=existing_job_id, created=False)

            self._session.add(
                JobOutbox(
                    outbox_id=self._outbox_id_factory(),
                    job_id=inserted_job_id,
                    status=OutboxStatus.READY,
                    attempt_count=0,
                )
            )
            await self._session.flush()
            return IngestionResult(job_id=inserted_job_id, created=True)

    async def get(self, job_id: UUID) -> AiJob | None:
        """외부 조회 API가 노출할 단일 작업을 식별자로 찾는다."""
        return await self._session.get(AiJob, job_id)


class IngestionService:
    """수신 경계와 저장소 사이에서 작업 접수 유스케이스를 조정한다."""

    def __init__(self, repository: JobRepository) -> None:
        """트랜잭션 책임을 가진 저장소를 주입한다."""
        self._repository = repository

    async def accept(self, incoming: IncomingJob) -> IngestionResult:
        """검증된 이벤트를 멱등한 작업과 Outbox로 접수한다."""
        return await self._repository.create_with_outbox(incoming)

    async def get_job(self, job_id: UUID) -> AiJob | None:
        """작업 상태 조회를 저장소에 위임한다."""
        return await self._repository.get(job_id)
