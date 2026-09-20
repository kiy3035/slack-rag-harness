from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.domain import JobStatus, OutboxStatus, ReviewStatus
from app.db.models import AiJob, AnswerCitation, JobOutbox, ReviewQueue
from app.reviews.schemas import (
    AllowedReviewCitation,
    EditApproveRequest,
    ReviewCitation,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
)


class ReviewNotFoundError(LookupError):
    """요청한 검토 식별자가 존재하지 않음을 나타낸다."""


class ReviewConflictError(RuntimeError):
    """현재 검토 상태나 근거가 요청한 결정과 맞지 않음을 나타낸다."""


class ReviewService:
    """검토 목록 조회와 승인·수정·재검색·반려 상태 전이를 조정한다."""

    def __init__(self, session: AsyncSession) -> None:
        """요청 단위 트랜잭션에 사용할 비동기 DB Session을 주입받는다."""
        self._session = session

    async def list_reviews(
        self,
        *,
        status: ReviewStatus,
        limit: int,
        offset: int,
    ) -> list[ReviewQueue]:
        """지정 상태의 검토 건을 오래된 순서와 제한 범위로 조회한다."""
        rows = await self._session.scalars(
            select(ReviewQueue)
            .where(ReviewQueue.status == status)
            .order_by(ReviewQueue.created_at, ReviewQueue.review_id)
            .limit(limit)
            .offset(offset)
        )
        return list(rows)

    async def get_review(self, review_id: UUID) -> ReviewQueue:
        """검토 한 건을 조회하고 없으면 명시적인 오류를 반환한다."""
        review = await self._session.get(ReviewQueue, review_id)
        if review is None:
            raise ReviewNotFoundError("REVIEW_NOT_FOUND")
        return review

    async def approve(
        self,
        review_id: UUID,
        request: ReviewDecisionRequest,
    ) -> ReviewDecisionResponse:
        """검증된 초안과 인용이 있는 WAITING 검토를 한 번만 승인한다."""
        async with self._session.begin():
            review = await self._lock_review(review_id)
            if review.status == ReviewStatus.APPROVED:
                return self._decision_response(
                    review, JobStatus.COMPLETED, idempotent=True
                )
            self._require_waiting(review)
            if not review.draft_answer:
                raise ReviewConflictError("REVIEW_DRAFT_MISSING")
            citations = self._resolve_citations(
                review.draft_citations, review.allowed_citations
            )
            if not citations:
                raise ReviewConflictError("REVIEW_CITATIONS_MISSING")
            await self._complete_job(
                review,
                answer=review.draft_answer,
                citations=citations,
            )
            self._mark_review(
                review,
                status=ReviewStatus.APPROVED,
                comment=request.comment,
            )
            return self._decision_response(
                review, JobStatus.COMPLETED, idempotent=False
            )

    async def edit_approve(
        self,
        review_id: UUID,
        request: EditApproveRequest,
    ) -> ReviewDecisionResponse:
        """수정 답변의 인용을 기존 검색 집합으로 제한해 한 번만 승인한다."""
        async with self._session.begin():
            review = await self._lock_review(review_id)
            if review.status == ReviewStatus.EDITED:
                return self._decision_response(
                    review, JobStatus.COMPLETED, idempotent=True
                )
            self._require_waiting(review)
            raw_citations = [citation.model_dump(mode="json") for citation in request.citations]
            citations = self._resolve_citations(
                raw_citations, review.allowed_citations
            )
            if len(citations) != len(request.citations):
                raise ReviewConflictError("REVIEW_CITATION_NOT_ALLOWED")
            await self._complete_job(
                review,
                answer=request.answer,
                citations=citations,
            )
            review.draft_answer = request.answer
            review.draft_citations = raw_citations
            self._mark_review(
                review,
                status=ReviewStatus.EDITED,
                comment=request.comment,
            )
            return self._decision_response(
                review, JobStatus.COMPLETED, idempotent=False
            )

    async def retry(
        self,
        review_id: UUID,
        request: ReviewDecisionRequest,
    ) -> ReviewDecisionResponse:
        """WAITING 검토의 Workflow 세대를 올리고 기존 Outbox를 재발행 가능하게 연다."""
        async with self._session.begin():
            review = await self._lock_review(review_id)
            if review.status == ReviewStatus.RETRY_REQUESTED:
                job_status = await self._job_status(review.job_id)
                return self._decision_response(review, job_status, idempotent=True)
            self._require_waiting(review)
            job_result = await self._session.execute(
                update(AiJob)
                .where(
                    AiJob.job_id == review.job_id,
                    AiJob.status == JobStatus.REVIEW_REQUIRED,
                    AiJob.workflow_revision == review.workflow_revision,
                )
                .values(
                    status=JobStatus.RECEIVED,
                    workflow_revision=AiJob.workflow_revision + 1,
                    result_answer=None,
                    failure_code=None,
                    failure_message=None,
                    next_retry_at=None,
                    locked_at=None,
                    started_at=None,
                    completed_at=None,
                )
            )
            if job_result.rowcount != 1:
                raise ReviewConflictError("REVIEW_JOB_STATE_CONFLICT")
            outbox_result = await self._session.execute(
                update(JobOutbox)
                .where(JobOutbox.job_id == review.job_id)
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
                raise ReviewConflictError("REVIEW_OUTBOX_NOT_FOUND")
            self._mark_review(
                review,
                status=ReviewStatus.RETRY_REQUESTED,
                comment=request.comment,
            )
            return self._decision_response(
                review, JobStatus.RECEIVED, idempotent=False
            )

    async def reject(
        self,
        review_id: UUID,
        request: ReviewDecisionRequest,
    ) -> ReviewDecisionResponse:
        """WAITING 검토와 작업을 조건부로 한 번만 반려 상태로 전이한다."""
        async with self._session.begin():
            review = await self._lock_review(review_id)
            if review.status == ReviewStatus.REJECTED:
                return self._decision_response(
                    review, JobStatus.REJECTED, idempotent=True
                )
            self._require_waiting(review)
            job_result = await self._session.execute(
                update(AiJob)
                .where(
                    AiJob.job_id == review.job_id,
                    AiJob.status == JobStatus.REVIEW_REQUIRED,
                )
                .values(
                    status=JobStatus.REJECTED,
                    result_answer=None,
                    locked_at=None,
                    completed_at=datetime.now(UTC),
                )
            )
            if job_result.rowcount != 1:
                raise ReviewConflictError("REVIEW_JOB_STATE_CONFLICT")
            self._mark_review(
                review,
                status=ReviewStatus.REJECTED,
                comment=request.comment,
            )
            return self._decision_response(
                review, JobStatus.REJECTED, idempotent=False
            )

    async def _lock_review(self, review_id: UUID) -> ReviewQueue:
        """동시 검토 결정을 직렬화하도록 대상 행을 배타적으로 조회한다."""
        review = await self._session.scalar(
            select(ReviewQueue)
            .where(ReviewQueue.review_id == review_id)
            .with_for_update()
        )
        if review is None:
            raise ReviewNotFoundError("REVIEW_NOT_FOUND")
        return review

    def _require_waiting(self, review: ReviewQueue) -> None:
        """이미 다른 결정이 끝난 검토에 상충하는 변경을 막는다."""
        if review.status != ReviewStatus.WAITING:
            raise ReviewConflictError("REVIEW_ALREADY_DECIDED")

    def _resolve_citations(
        self,
        requested: list[dict[str, object]],
        allowed: list[dict[str, object]],
    ) -> list[AllowedReviewCitation]:
        """요청 인용을 원 Workflow 검색 집합과 점수에 정확히 대응시킨다."""
        allowed_models = [AllowedReviewCitation.model_validate(item) for item in allowed]
        allowed_by_pair = {
            (item.document_id, item.chunk_id): item for item in allowed_models
        }
        resolved: list[AllowedReviewCitation] = []
        for item in requested:
            citation = ReviewCitation.model_validate(item)
            matched = allowed_by_pair.get((citation.document_id, citation.chunk_id))
            if matched is not None and matched not in resolved:
                resolved.append(matched)
        return resolved

    async def _complete_job(
        self,
        review: ReviewQueue,
        *,
        answer: str,
        citations: list[AllowedReviewCitation],
    ) -> None:
        """검토 답변·완료 상태·검증된 인용을 같은 트랜잭션에 저장한다."""
        job_result = await self._session.execute(
            update(AiJob)
            .where(
                AiJob.job_id == review.job_id,
                AiJob.status == JobStatus.REVIEW_REQUIRED,
            )
            .values(
                status=JobStatus.COMPLETED,
                result_answer=answer,
                failure_code=None,
                failure_message=None,
                locked_at=None,
                completed_at=datetime.now(UTC),
            )
        )
        if job_result.rowcount != 1:
            raise ReviewConflictError("REVIEW_JOB_STATE_CONFLICT")
        await self._session.execute(
            delete(AnswerCitation).where(AnswerCitation.job_id == review.job_id)
        )
        for citation in citations:
            self._session.add(
                AnswerCitation(
                    citation_id=uuid4(),
                    job_id=review.job_id,
                    document_id=citation.document_id,
                    chunk_id=citation.chunk_id,
                    similarity_score=citation.similarity_score,
                )
            )

    def _mark_review(
        self,
        review: ReviewQueue,
        *,
        status: ReviewStatus,
        comment: str | None,
    ) -> None:
        """검토 행에 최종 결정과 의견·시각을 함께 반영한다."""
        review.status = status
        review.review_comment = comment
        review.reviewed_at = datetime.now(UTC)

    async def _job_status(self, job_id: UUID) -> JobStatus:
        """멱등 재호출 응답에 사용할 현재 작업 상태를 조회한다."""
        status = await self._session.scalar(
            select(AiJob.status).where(AiJob.job_id == job_id)
        )
        if status is None:
            raise ReviewConflictError("REVIEW_JOB_NOT_FOUND")
        return status

    def _decision_response(
        self,
        review: ReviewQueue,
        job_status: JobStatus,
        *,
        idempotent: bool,
    ) -> ReviewDecisionResponse:
        """검토 결정 API의 일관된 상태 응답을 생성한다."""
        return ReviewDecisionResponse(
            review_id=review.review_id,
            job_id=review.job_id,
            review_status=review.status,
            job_status=job_status,
            idempotent=idempotent,
        )
