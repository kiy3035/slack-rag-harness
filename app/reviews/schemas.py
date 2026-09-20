from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.common.domain import JobStatus, ReviewStatus


class ReviewCitation(BaseModel):
    """검토 초안 또는 수정 답변이 참조할 문서·Chunk 식별자다."""

    document_id: UUID
    chunk_id: UUID


class AllowedReviewCitation(ReviewCitation):
    """원 Workflow 검색에서 확인된 인용과 유사도 점수를 표현한다."""

    similarity_score: float = Field(ge=-1.0, le=1.0)


class ReviewResponse(BaseModel):
    """질문 원문을 노출하지 않고 검토 상태와 근거 후보를 반환한다."""

    model_config = ConfigDict(from_attributes=True)

    review_id: UUID
    job_id: UUID
    workflow_revision: int
    reason_code: str
    draft_answer: str | None
    draft_citations: list[ReviewCitation]
    allowed_citations: list[AllowedReviewCitation]
    status: ReviewStatus
    review_comment: str | None
    created_at: datetime
    reviewed_at: datetime | None


class ReviewListResponse(BaseModel):
    """상태별 검토 목록과 반환 건수를 함께 제공한다."""

    items: list[ReviewResponse]
    count: int


class ReviewDecisionRequest(BaseModel):
    """승인·재검색·반려 시 선택적으로 남기는 검토 의견을 검증한다."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    comment: str | None = Field(default=None, max_length=1_000)


class EditApproveRequest(ReviewDecisionRequest):
    """수정 승인할 답변과 검색 근거 인용을 검증한다."""

    answer: str = Field(min_length=1, max_length=8_000)
    citations: list[ReviewCitation] = Field(min_length=1, max_length=20)


class ReviewDecisionResponse(BaseModel):
    """검토 결정 이후 검토·작업 상태와 멱등 재호출 여부를 반환한다."""

    review_id: UUID
    job_id: UUID
    review_status: ReviewStatus
    job_status: JobStatus
    idempotent: bool
