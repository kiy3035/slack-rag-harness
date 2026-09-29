from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.domain import JobStatus, ReviewStatus
from app.db.models import AiJob, AnswerCitation, ReviewQueue
from app.integrations.slack.repository import enqueue_slack_reply
from app.workflow.schemas import (
    ReviewReasonCode,
    WorkflowJob,
    WorkflowResult,
)


class WorkflowRepository:
    """Workflow 작업 조회와 최종 답변·검토 결과를 원자적으로 저장한다."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """노드 외부에서 독립 DB 트랜잭션을 만들 세션 팩터리를 보관한다."""
        self._session_factory = session_factory

    async def get_job(self, job_id: UUID) -> WorkflowJob | None:
        """Worker가 실행할 질문·상태·Workflow 세대만 조회한다."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        AiJob.job_id,
                        AiJob.question,
                        AiJob.status,
                        AiJob.workflow_revision,
                    ).where(AiJob.job_id == job_id)
                )
            ).mappings().one_or_none()
        return WorkflowJob.model_validate(dict(row)) if row is not None else None

    async def record_outcome(
        self,
        result: WorkflowResult,
        *,
        now: datetime | None = None,
    ) -> bool:
        """PROCESSING 작업의 답변 인용 또는 검토 요청을 한 트랜잭션으로 기록한다."""
        if result.status not in {JobStatus.COMPLETED, JobStatus.REVIEW_REQUIRED}:
            raise ValueError("Workflow 종료 상태가 저장 가능한 값이 아닙니다.")
        recorded_at = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            changed = await self._update_job(session, result, recorded_at)
            if not changed:
                current_status = await session.scalar(
                    select(AiJob.status).where(AiJob.job_id == result.job_id)
                )
                return current_status == result.status
            if result.status == JobStatus.COMPLETED:
                await self._replace_citations(session, result)
                if result.answer is None:
                    raise ValueError("완료 결과에는 Slack 발신용 답변이 필요합니다.")
                await enqueue_slack_reply(
                    session,
                    job_id=result.job_id,
                    answer=result.answer.answer,
                )
            else:
                await self._upsert_review(session, result)
            return True

    async def _update_job(
        self,
        session: AsyncSession,
        result: WorkflowResult,
        recorded_at: datetime,
    ) -> bool:
        """현재 Workflow 세대의 PROCESSING 작업만 최종 상태로 전이한다."""
        answer_text = (
            result.answer.answer
            if result.status == JobStatus.COMPLETED and result.answer is not None
            else None
        )
        completed_at = recorded_at if result.status == JobStatus.COMPLETED else None
        update_result = await session.execute(
            update(AiJob)
            .where(
                AiJob.job_id == result.job_id,
                AiJob.status == JobStatus.PROCESSING,
                AiJob.workflow_revision == result.workflow_revision,
            )
            .values(
                status=result.status,
                result_answer=answer_text,
                failure_code=None,
                failure_message=None,
                locked_at=None,
                completed_at=completed_at,
            )
        )
        return update_result.rowcount == 1

    async def _replace_citations(
        self,
        session: AsyncSession,
        result: WorkflowResult,
    ) -> None:
        """최종 답변이 인용한 검색 Chunk와 실제 검색 점수를 교체 저장한다."""
        if result.answer is None:
            raise ValueError("완료 결과에는 답변이 필요합니다.")
        score_by_pair = {
            (chunk.document_id, chunk.chunk_id): chunk.score
            for chunk in result.relevant_chunks
        }
        await session.execute(
            delete(AnswerCitation).where(AnswerCitation.job_id == result.job_id)
        )
        persisted_pairs: set[tuple[UUID, UUID]] = set()
        for citation in result.answer.citations:
            pair = (citation.document_id, citation.chunk_id)
            if pair not in score_by_pair:
                raise ValueError("최종 인용이 검색 결과와 일치하지 않습니다.")
            if pair in persisted_pairs:
                continue
            persisted_pairs.add(pair)
            session.add(
                AnswerCitation(
                    citation_id=uuid4(),
                    job_id=result.job_id,
                    document_id=citation.document_id,
                    chunk_id=citation.chunk_id,
                    similarity_score=score_by_pair[pair],
                )
            )

    async def _upsert_review(
        self,
        session: AsyncSession,
        result: WorkflowResult,
    ) -> None:
        """같은 작업 세대의 검토 요청이 중복 실행돼도 한 건으로 수렴시킨다."""
        answer = result.answer
        draft_citations = (
            [citation.model_dump(mode="json") for citation in answer.citations]
            if answer is not None
            else []
        )
        allowed_citations = [
            {
                "document_id": str(chunk.document_id),
                "chunk_id": str(chunk.chunk_id),
                "similarity_score": chunk.score,
            }
            for chunk in result.relevant_chunks
        ]
        reason = result.review_reason_code or ReviewReasonCode.MODEL_REVIEW_REQUIRED
        statement = (
            insert(ReviewQueue)
            .values(
                review_id=uuid4(),
                job_id=result.job_id,
                workflow_revision=result.workflow_revision,
                reason_code=reason.value,
                draft_answer=answer.answer if answer is not None else None,
                draft_citations=draft_citations,
                allowed_citations=allowed_citations,
                status=ReviewStatus.WAITING,
            )
            .on_conflict_do_nothing(
                index_elements=["job_id", "workflow_revision"]
            )
        )
        await session.execute(statement)
