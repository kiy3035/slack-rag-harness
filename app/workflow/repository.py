from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.domain import JobStatus
from app.db.models import AiJob
from app.workflow.schemas import AnswerOutput, WorkflowJob


class WorkflowRepository:
    """Workflow 입력 조회와 최종 작업 상태 전이를 짧은 트랜잭션으로 처리한다."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """노드 외부에서 독립 DB 트랜잭션을 만들 세션 팩터리를 보관한다."""
        self._session_factory = session_factory

    async def get_job(self, job_id: UUID) -> WorkflowJob | None:
        """Worker가 실행할 질문과 현재 상태만 작업 테이블에서 조회한다."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AiJob.job_id, AiJob.question, AiJob.status).where(
                        AiJob.job_id == job_id
                    )
                )
            ).mappings().one_or_none()
        return WorkflowJob.model_validate(dict(row)) if row is not None else None

    async def record_outcome(
        self,
        job_id: UUID,
        *,
        status: JobStatus,
        answer: AnswerOutput | None,
        now: datetime | None = None,
    ) -> bool:
        """PROCESSING 작업만 완료 또는 검토 상태로 조건부 전이한다."""
        if status not in {JobStatus.COMPLETED, JobStatus.REVIEW_REQUIRED}:
            raise ValueError("Workflow 종료 상태가 저장 가능한 값이 아닙니다.")
        completed_at = (now or datetime.now(UTC)) if status == JobStatus.COMPLETED else None
        result_answer = answer.answer if answer is not None else None
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(AiJob)
                .where(
                    AiJob.job_id == job_id,
                    AiJob.status == JobStatus.PROCESSING,
                )
                .values(
                    status=status,
                    result_answer=result_answer,
                    failure_code=None,
                    failure_message=None,
                    locked_at=None,
                    completed_at=completed_at,
                )
            )
            if result.rowcount == 1:
                return True
            current_status = await session.scalar(
                select(AiJob.status).where(AiJob.job_id == job_id)
            )
            return current_status == status
