from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.common.domain import JobStatus, OutboxStatus
from app.db.models import AiJob, JobOutbox


@dataclass(frozen=True, slots=True)
class ClaimedOutbox:
    """Lease 소유권 검증에 필요한 Outbox와 추적 필드를 보관한다."""

    outbox_id: UUID
    job_id: UUID
    request_id: str
    thread_id: str | None
    attempt_count: int
    locked_at: datetime


@dataclass(frozen=True, slots=True)
class FailureResult:
    """발행 실패가 재시도 또는 최종 실패로 저장됐는지 나타낸다."""

    updated: bool
    exhausted: bool


class OutboxRepository:
    """Outbox 선점과 상태 전이를 짧은 PostgreSQL 트랜잭션으로 수행한다."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """호출별 독립 트랜잭션을 만들 세션 팩터리를 주입한다."""
        self._session_factory = session_factory

    async def claim_ready(self, *, now: datetime, limit: int) -> list[ClaimedOutbox]:
        """재시도 시각이 지난 READY 행을 잠금 충돌 없이 조건부 선점한다."""
        async with self._session_factory() as session, session.begin():
            rows = (
                await session.execute(self._ready_query(now=now, limit=limit))
            ).all()
            if not rows:
                return []
            outbox_ids = [row.outbox_id for row in rows]
            await session.execute(
                update(JobOutbox)
                .where(
                    JobOutbox.outbox_id.in_(outbox_ids),
                    JobOutbox.status == OutboxStatus.READY,
                )
                .values(status=OutboxStatus.PROCESSING, locked_at=now)
            )
            return [
                ClaimedOutbox(
                    outbox_id=row.outbox_id,
                    job_id=row.job_id,
                    request_id=row.request_id,
                    thread_id=row.thread_id,
                    attempt_count=row.attempt_count,
                    locked_at=now,
                )
                for row in rows
            ]

    async def mark_sent(self, claim: ClaimedOutbox, *, now: datetime) -> bool:
        """현재 Lease 소유자만 발행 완료와 작업 QUEUED 전이를 기록한다."""
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(JobOutbox)
                .where(self._owned_processing_clause(claim))
                .values(
                    status=OutboxStatus.SENT,
                    sent_at=now,
                    locked_at=None,
                    next_retry_at=None,
                    last_error=None,
                )
            )
            if result.rowcount != 1:
                return False
            await session.execute(
                update(AiJob)
                .where(
                    AiJob.job_id == claim.job_id,
                    AiJob.status == JobStatus.RECEIVED,
                )
                .values(status=JobStatus.QUEUED)
            )
            return True

    async def record_failure(
        self,
        claim: ClaimedOutbox,
        *,
        now: datetime,
        next_retry_at: datetime,
        error: str,
        max_attempts: int,
    ) -> FailureResult:
        """현재 Lease의 실패 횟수와 제한된 재시도 시각을 원자적으로 저장한다."""
        next_attempt_count = claim.attempt_count + 1
        exhausted = next_attempt_count >= max_attempts
        values: dict[str, object] = {
            "status": OutboxStatus.FAIL if exhausted else OutboxStatus.READY,
            "attempt_count": next_attempt_count,
            "last_error": error[:500],
            "next_retry_at": None if exhausted else next_retry_at,
            "locked_at": None,
        }
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(JobOutbox)
                .where(self._owned_processing_clause(claim))
                .values(**values)
            )
            if result.rowcount != 1:
                return FailureResult(updated=False, exhausted=exhausted)
            if exhausted:
                await session.execute(
                    update(AiJob)
                    .where(
                        AiJob.job_id == claim.job_id,
                        AiJob.status.in_([JobStatus.RECEIVED, JobStatus.QUEUED]),
                    )
                    .values(
                        status=JobStatus.FAILED,
                        failure_code="OUTBOX_PUBLISH_EXHAUSTED",
                        failure_message=error[:500],
                        completed_at=now,
                    )
                )
            return FailureResult(updated=True, exhausted=exhausted)

    async def recover_stale(
        self,
        *,
        now: datetime,
        lease_timeout: timedelta,
        max_attempts: int,
    ) -> tuple[int, int]:
        """만료된 PROCESSING Lease를 재시도 가능 또는 최종 실패로 복구한다."""
        cutoff = now - lease_timeout
        recovered = 0
        exhausted = 0
        async with self._session_factory() as session, session.begin():
            rows = (
                await session.execute(
                    select(JobOutbox.outbox_id, JobOutbox.job_id, JobOutbox.attempt_count)
                    .where(
                        JobOutbox.status == OutboxStatus.PROCESSING,
                        JobOutbox.locked_at <= cutoff,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                next_attempt_count = row.attempt_count + 1
                is_exhausted = next_attempt_count >= max_attempts
                await session.execute(
                    update(JobOutbox)
                    .where(
                        JobOutbox.outbox_id == row.outbox_id,
                        JobOutbox.status == OutboxStatus.PROCESSING,
                        JobOutbox.locked_at <= cutoff,
                    )
                    .values(
                        status=OutboxStatus.FAIL if is_exhausted else OutboxStatus.READY,
                        attempt_count=next_attempt_count,
                        last_error="OUTBOX_LEASE_EXPIRED",
                        next_retry_at=None if is_exhausted else now,
                        locked_at=None,
                    )
                )
                if is_exhausted:
                    exhausted += 1
                    await session.execute(
                        update(AiJob)
                        .where(
                            AiJob.job_id == row.job_id,
                            AiJob.status.in_([JobStatus.RECEIVED, JobStatus.QUEUED]),
                        )
                        .values(
                            status=JobStatus.FAILED,
                            failure_code="OUTBOX_LEASE_EXHAUSTED",
                            failure_message="Outbox 발행 Lease 만료 복구 횟수를 소진했습니다.",
                            completed_at=now,
                        )
                    )
                else:
                    recovered += 1
        return recovered, exhausted

    def _ready_query(self, *, now: datetime, limit: int) -> Select[tuple[UUID, UUID, str, str | None, int]]:
        """공정한 순서와 행 잠금을 적용한 READY 조회문을 만든다."""
        return (
            select(
                JobOutbox.outbox_id,
                JobOutbox.job_id,
                AiJob.external_event_id.label("request_id"),
                AiJob.slack_thread_ts.label("thread_id"),
                JobOutbox.attempt_count,
            )
            .join(AiJob, AiJob.job_id == JobOutbox.job_id)
            .where(
                JobOutbox.status == OutboxStatus.READY,
                or_(JobOutbox.next_retry_at.is_(None), JobOutbox.next_retry_at <= now),
            )
            .order_by(JobOutbox.created_at, JobOutbox.outbox_id)
            .limit(limit)
            .with_for_update(of=JobOutbox, skip_locked=True)
        )

    def _owned_processing_clause(self, claim: ClaimedOutbox) -> ColumnElement[bool]:
        """오래된 Publisher가 새 Lease 결과를 덮어쓰지 못하게 소유권 조건을 만든다."""
        return and_(
            JobOutbox.outbox_id == claim.outbox_id,
            JobOutbox.status == OutboxStatus.PROCESSING,
            JobOutbox.locked_at == claim.locked_at,
        )
