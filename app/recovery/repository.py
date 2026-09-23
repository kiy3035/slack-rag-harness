from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.domain import JobStatus, OutboxStatus, ReviewStatus
from app.db.models import AiJob, JobOutbox, ReviewQueue
from app.recovery.errors import FailureDecision, FailureKind, RetryPolicy


@dataclass(frozen=True, slots=True)
class FailureTransition:
    """Worker 실패가 DB에 적용된 상태와 다음 재시도 시각을 나타낸다."""

    updated: bool
    status: JobStatus | None
    error_code: str
    next_retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StaleRecoveryResult:
    """오래된 PROCESSING 작업의 재시도와 소진 건수를 반환한다."""

    retried: int
    exhausted: int


@dataclass(frozen=True, slots=True)
class DeadLetterClaim:
    """DLQ 발행 Lease와 원본 작업 메시지 재구성 필드를 보관한다."""

    outbox_id: UUID
    job_id: UUID
    request_id: str
    thread_id: str | None
    error_code: str
    attempt_count: int
    locked_at: datetime


class JobRecoveryRepository:
    """Worker 실행 재시도와 DLQ 발행 상태를 짧은 트랜잭션으로 관리한다."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """복구 동작마다 독립 트랜잭션을 만들 세션 팩터리를 주입한다."""
        self._session_factory = session_factory

    async def claim(self, job_id: UUID, *, now: datetime) -> bool:
        """대기 작업 한 건만 PROCESSING으로 조건부 전이하고 실행 횟수를 올린다."""
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(AiJob)
                .where(
                    AiJob.job_id == job_id,
                    AiJob.status.in_([JobStatus.RECEIVED, JobStatus.QUEUED]),
                )
                .values(
                    status=JobStatus.PROCESSING,
                    attempt_count=AiJob.attempt_count + 1,
                    locked_at=now,
                    started_at=now,
                    completed_at=None,
                )
            )
            return result.rowcount == 1

    async def record_failure(
        self,
        job_id: UUID,
        *,
        decision: FailureDecision,
        policy: RetryPolicy,
        now: datetime,
    ) -> FailureTransition:
        """선점된 작업 오류를 검토, 재시도 대기, DLQ 중 하나로 원자 전이한다."""
        async with self._session_factory() as session, session.begin():
            job = await session.scalar(
                select(AiJob)
                .where(
                    AiJob.job_id == job_id,
                    AiJob.status == JobStatus.PROCESSING,
                )
                .with_for_update()
            )
            if job is None:
                return FailureTransition(
                    updated=False,
                    status=None,
                    error_code=decision.error_code,
                )
            if decision.kind == FailureKind.REVIEW_REQUIRED:
                await self._mark_review_required(session, job, decision, now)
                return FailureTransition(
                    updated=True,
                    status=JobStatus.REVIEW_REQUIRED,
                    error_code=decision.error_code,
                )
            if (
                decision.kind == FailureKind.TRANSIENT
                and job.attempt_count < policy.max_attempts
            ):
                retry_at = now + timedelta(
                    seconds=policy.delay_seconds(job.attempt_count)
                )
                job.status = JobStatus.RETRY_WAIT
                job.failure_code = decision.error_code
                job.failure_message = decision.safe_message
                job.next_retry_at = retry_at
                job.locked_at = None
                job.completed_at = None
                return FailureTransition(
                    updated=True,
                    status=JobStatus.RETRY_WAIT,
                    error_code=decision.error_code,
                    next_retry_at=retry_at,
                )
            error_code = decision.error_code
            if decision.kind == FailureKind.TRANSIENT:
                error_code = f"WORKER_RETRY_EXHAUSTED_{decision.error_code}"
            self._mark_dead_letter(
                job,
                error_code=error_code,
                safe_message=decision.safe_message,
                now=now,
            )
            return FailureTransition(
                updated=True,
                status=JobStatus.DEAD_LETTER,
                error_code=error_code,
            )

    async def release_due_retries(self, *, now: datetime, limit: int) -> int:
        """Backoff가 끝난 작업과 기존 Outbox를 다시 발행 가능한 상태로 연다."""
        async with self._session_factory() as session, session.begin():
            job_ids = list(
                await session.scalars(
                    select(AiJob.job_id)
                    .where(
                        AiJob.status == JobStatus.RETRY_WAIT,
                        AiJob.next_retry_at <= now,
                    )
                    .order_by(AiJob.next_retry_at, AiJob.job_id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            if not job_ids:
                return 0
            outbox_result = await session.execute(
                update(JobOutbox)
                .where(JobOutbox.job_id.in_(job_ids))
                .values(
                    status=OutboxStatus.READY,
                    attempt_count=0,
                    last_error=None,
                    next_retry_at=None,
                    locked_at=None,
                    sent_at=None,
                )
            )
            if outbox_result.rowcount != len(job_ids):
                raise RuntimeError("WORKER_RETRY_OUTBOX_MISSING")
            job_result = await session.execute(
                update(AiJob)
                .where(
                    AiJob.job_id.in_(job_ids),
                    AiJob.status == JobStatus.RETRY_WAIT,
                )
                .values(
                    status=JobStatus.RECEIVED,
                    next_retry_at=None,
                    locked_at=None,
                    completed_at=None,
                )
            )
            return job_result.rowcount

    async def recover_stale_processing(
        self,
        *,
        now: datetime,
        lease_timeout: timedelta,
        policy: RetryPolicy,
        limit: int,
    ) -> StaleRecoveryResult:
        """Worker 종료로 만료된 PROCESSING 작업을 제한 재시도 또는 DLQ로 보낸다."""
        cutoff = now - lease_timeout
        retried = 0
        exhausted = 0
        async with self._session_factory() as session, session.begin():
            jobs = list(
                await session.scalars(
                    select(AiJob)
                    .where(
                        AiJob.status == JobStatus.PROCESSING,
                        AiJob.locked_at <= cutoff,
                    )
                    .order_by(AiJob.locked_at, AiJob.job_id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for job in jobs:
                if job.attempt_count >= policy.max_attempts:
                    self._mark_dead_letter(
                        job,
                        error_code="WORKER_LEASE_EXHAUSTED",
                        safe_message="Worker 중단 복구 횟수를 소진했습니다.",
                        now=now,
                    )
                    exhausted += 1
                    continue
                job.status = JobStatus.RETRY_WAIT
                job.failure_code = "WORKER_LEASE_EXPIRED"
                job.failure_message = "중단된 Worker 작업을 재시도 대기로 복구했습니다."
                job.next_retry_at = now + timedelta(
                    seconds=policy.delay_seconds(job.attempt_count)
                )
                job.locked_at = None
                job.completed_at = None
                retried += 1
        return StaleRecoveryResult(retried=retried, exhausted=exhausted)

    async def claim_pending_dead_letters(
        self,
        *,
        now: datetime,
        lease_timeout: timedelta,
        limit: int,
    ) -> list[DeadLetterClaim]:
        """미발행 DLQ 작업을 Lease로 선점해 여러 Worker의 중복 발행을 줄인다."""
        cutoff = now - lease_timeout
        async with self._session_factory() as session, session.begin():
            rows = (
                await session.execute(
                    select(
                        AiJob.job_id,
                        AiJob.external_event_id.label("request_id"),
                        AiJob.slack_thread_ts.label("thread_id"),
                        AiJob.failure_code,
                        AiJob.dlq_attempt_count,
                        JobOutbox.outbox_id,
                    )
                    .join(JobOutbox, JobOutbox.job_id == AiJob.job_id)
                    .where(
                        AiJob.status == JobStatus.DEAD_LETTER,
                        AiJob.dlq_published_at.is_(None),
                        or_(
                            AiJob.dlq_next_retry_at.is_(None),
                            AiJob.dlq_next_retry_at <= now,
                        ),
                        or_(AiJob.locked_at.is_(None), AiJob.locked_at <= cutoff),
                    )
                    .order_by(AiJob.completed_at, AiJob.job_id)
                    .limit(limit)
                    .with_for_update(of=AiJob, skip_locked=True)
                )
            ).all()
            if not rows:
                return []
            job_ids = [row.job_id for row in rows]
            await session.execute(
                update(AiJob)
                .where(
                    AiJob.job_id.in_(job_ids),
                    AiJob.status == JobStatus.DEAD_LETTER,
                    AiJob.dlq_published_at.is_(None),
                )
                .values(locked_at=now)
            )
            return [
                DeadLetterClaim(
                    outbox_id=row.outbox_id,
                    job_id=row.job_id,
                    request_id=row.request_id,
                    thread_id=row.thread_id,
                    error_code=row.failure_code or "WORKER_FAILURE",
                    attempt_count=row.dlq_attempt_count,
                    locked_at=now,
                )
                for row in rows
            ]

    async def mark_dead_letter_published(
        self,
        claim: DeadLetterClaim,
        *,
        now: datetime,
    ) -> bool:
        """현재 DLQ Lease 소유자만 발행 완료 시각을 기록한다."""
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(AiJob)
                .where(
                    AiJob.job_id == claim.job_id,
                    AiJob.status == JobStatus.DEAD_LETTER,
                    AiJob.dlq_published_at.is_(None),
                    AiJob.locked_at == claim.locked_at,
                )
                .values(
                    dlq_published_at=now,
                    dlq_next_retry_at=None,
                    dlq_last_error=None,
                    locked_at=None,
                )
            )
            return result.rowcount == 1

    async def record_dead_letter_publish_failure(
        self,
        claim: DeadLetterClaim,
        *,
        now: datetime,
        next_retry_at: datetime,
        error_code: str,
    ) -> bool:
        """DLQ 발행 실패 횟수와 다음 재시도 시각을 민감 정보 없이 저장한다."""
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(AiJob)
                .where(
                    AiJob.job_id == claim.job_id,
                    AiJob.status == JobStatus.DEAD_LETTER,
                    AiJob.dlq_published_at.is_(None),
                    AiJob.locked_at == claim.locked_at,
                )
                .values(
                    dlq_attempt_count=claim.attempt_count + 1,
                    dlq_last_error=error_code[:100],
                    dlq_next_retry_at=next_retry_at,
                    locked_at=None,
                )
            )
            return result.rowcount == 1

    async def _mark_review_required(
        self,
        session: AsyncSession,
        job: AiJob,
        decision: FailureDecision,
        now: datetime,
    ) -> None:
        """출력 계약 오류를 빈 근거의 사람 검토 항목으로 안전하게 전환한다."""
        job.status = JobStatus.REVIEW_REQUIRED
        job.failure_code = decision.error_code
        job.failure_message = decision.safe_message
        job.next_retry_at = None
        job.locked_at = None
        job.completed_at = None
        await session.execute(
            insert(ReviewQueue)
            .values(
                review_id=uuid4(),
                job_id=job.job_id,
                workflow_revision=job.workflow_revision,
                reason_code=decision.error_code,
                draft_answer=None,
                draft_citations=[],
                allowed_citations=[],
                status=ReviewStatus.WAITING,
            )
            .on_conflict_do_nothing(
                index_elements=["job_id", "workflow_revision"]
            )
        )

    def _mark_dead_letter(
        self,
        job: AiJob,
        *,
        error_code: str,
        safe_message: str,
        now: datetime,
    ) -> None:
        """영구 또는 소진 오류를 DLQ 발행 대기 상태와 함께 기록한다."""
        job.status = JobStatus.DEAD_LETTER
        job.failure_code = error_code[:100]
        job.failure_message = safe_message[:500]
        job.next_retry_at = None
        job.locked_at = None
        job.completed_at = now
        job.dlq_attempt_count = 0
        job.dlq_last_error = None
        job.dlq_next_retry_at = now
        job.dlq_published_at = None
