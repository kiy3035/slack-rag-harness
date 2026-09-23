from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.domain import JobStatus, OutboxStatus
from app.db.models import AiJob, JobOutbox, JobRecoveryRequest


class ManualRecoveryNotFoundError(LookupError):
    """수동 재처리 대상 작업이 존재하지 않음을 나타낸다."""


class ManualRecoveryConflictError(RuntimeError):
    """현재 작업 상태가 수동 재처리를 허용하지 않음을 나타낸다."""


@dataclass(frozen=True, slots=True)
class ManualRecoveryResult:
    """수동 재처리 후 작업 상태와 멱등 여부를 전달한다."""

    job_id: UUID
    status: JobStatus
    workflow_revision: int
    idempotent: bool


class ManualRecoveryService:
    """실패 작업을 새 Workflow 세대로 멱등하게 재처리 요청한다."""

    def __init__(self, session: AsyncSession) -> None:
        """요청 단위 트랜잭션에 사용할 DB Session을 주입한다."""
        self._session = session

    async def retry(
        self,
        job_id: UUID,
        *,
        idempotency_key: str,
        reason: str | None,
    ) -> ManualRecoveryResult:
        """FAILED 또는 DEAD_LETTER 작업과 Outbox를 한 트랜잭션으로 다시 연다."""
        async with self._session.begin():
            job = await self._session.scalar(
                select(AiJob).where(AiJob.job_id == job_id).with_for_update()
            )
            if job is None:
                raise ManualRecoveryNotFoundError("JOB_NOT_FOUND")
            existing = await self._session.scalar(
                select(JobRecoveryRequest).where(
                    JobRecoveryRequest.job_id == job_id,
                    JobRecoveryRequest.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return ManualRecoveryResult(
                    job_id=job.job_id,
                    status=job.status,
                    workflow_revision=existing.resulting_workflow_revision,
                    idempotent=True,
                )
            if job.status not in {JobStatus.FAILED, JobStatus.DEAD_LETTER}:
                raise ManualRecoveryConflictError("JOB_NOT_RETRYABLE")

            from_status = job.status
            next_revision = job.workflow_revision + 1
            outbox_result = await self._session.execute(
                update(JobOutbox)
                .where(JobOutbox.job_id == job_id)
                .values(
                    status=OutboxStatus.READY,
                    attempt_count=0,
                    last_error=None,
                    next_retry_at=None,
                    locked_at=None,
                    sent_at=None,
                )
            )
            if outbox_result.rowcount != 1:
                raise ManualRecoveryConflictError("JOB_OUTBOX_NOT_FOUND")

            job.status = JobStatus.RECEIVED
            job.attempt_count = 0
            job.workflow_revision = next_revision
            job.result_answer = None
            job.failure_code = None
            job.failure_message = None
            job.next_retry_at = None
            job.locked_at = None
            job.started_at = None
            job.completed_at = None
            job.dlq_attempt_count = 0
            job.dlq_last_error = None
            job.dlq_next_retry_at = None
            job.dlq_published_at = None
            self._session.add(
                JobRecoveryRequest(
                    recovery_request_id=uuid4(),
                    job_id=job_id,
                    idempotency_key=idempotency_key,
                    from_status=from_status,
                    resulting_workflow_revision=next_revision,
                    reason=reason,
                )
            )
            return ManualRecoveryResult(
                job_id=job_id,
                status=JobStatus.RECEIVED,
                workflow_revision=next_revision,
                idempotent=False,
            )
