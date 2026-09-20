from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.dependencies import get_review_service
from app.common.domain import ReviewStatus
from app.reviews.schemas import (
    EditApproveRequest,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewListResponse,
    ReviewResponse,
)
from app.reviews.service import (
    ReviewConflictError,
    ReviewNotFoundError,
    ReviewService,
)


router = APIRouter(prefix="/api/v1/reviews", tags=["reviews"])


def raise_review_http_error(error: Exception) -> NoReturn:
    """검토 도메인 오류를 외부에 Stack Trace 없이 HTTP 오류 코드로 변환한다."""
    if isinstance(error, ReviewNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=str(error),
    ) from error


@router.get("", response_model=ReviewListResponse)
async def list_reviews(
    service: Annotated[ReviewService, Depends(get_review_service)],
    review_status: Annotated[ReviewStatus, Query(alias="status")] = ReviewStatus.WAITING,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReviewListResponse:
    """상태별 사람 검토 목록을 오래된 순서로 조회한다."""
    reviews = await service.list_reviews(
        status=review_status,
        limit=limit,
        offset=offset,
    )
    items = [ReviewResponse.model_validate(review) for review in reviews]
    return ReviewListResponse(items=items, count=len(items))


@router.get("/{review_id}", response_model=ReviewResponse)
async def get_review(
    review_id: UUID,
    service: Annotated[ReviewService, Depends(get_review_service)],
) -> ReviewResponse:
    """검토 식별자로 초안과 허용된 인용 후보를 조회한다."""
    try:
        review = await service.get_review(review_id)
    except ReviewNotFoundError as error:
        raise_review_http_error(error)
    return ReviewResponse.model_validate(review)


@router.post("/{review_id}/approve", response_model=ReviewDecisionResponse)
async def approve_review(
    review_id: UUID,
    payload: ReviewDecisionRequest,
    service: Annotated[ReviewService, Depends(get_review_service)],
) -> ReviewDecisionResponse:
    """검증된 기존 초안을 최종 답변으로 멱등 승인한다."""
    try:
        return await service.approve(review_id, payload)
    except (ReviewNotFoundError, ReviewConflictError) as error:
        raise_review_http_error(error)


@router.post("/{review_id}/edit-approve", response_model=ReviewDecisionResponse)
async def edit_approve_review(
    review_id: UUID,
    payload: EditApproveRequest,
    service: Annotated[ReviewService, Depends(get_review_service)],
) -> ReviewDecisionResponse:
    """수정 답변과 허용 검색 인용을 최종 결과로 멱등 승인한다."""
    try:
        return await service.edit_approve(review_id, payload)
    except (ReviewNotFoundError, ReviewConflictError) as error:
        raise_review_http_error(error)


@router.post("/{review_id}/retry", response_model=ReviewDecisionResponse)
async def retry_review(
    review_id: UUID,
    payload: ReviewDecisionRequest,
    service: Annotated[ReviewService, Depends(get_review_service)],
) -> ReviewDecisionResponse:
    """검토 작업의 새 Workflow 세대를 Outbox로 멱등 재검색 요청한다."""
    try:
        return await service.retry(review_id, payload)
    except (ReviewNotFoundError, ReviewConflictError) as error:
        raise_review_http_error(error)


@router.post("/{review_id}/reject", response_model=ReviewDecisionResponse)
async def reject_review(
    review_id: UUID,
    payload: ReviewDecisionRequest,
    service: Annotated[ReviewService, Depends(get_review_service)],
) -> ReviewDecisionResponse:
    """자동 생성 결과를 저장하지 않고 검토 작업을 멱등 반려한다."""
    try:
        return await service.reject(review_id, payload)
    except (ReviewNotFoundError, ReviewConflictError) as error:
        raise_review_http_error(error)
