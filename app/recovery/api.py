from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_manual_recovery_service
from app.common.config import Settings, get_settings
from app.recovery.schemas import ManualRetryRequest, ManualRetryResponse
from app.recovery.service import (
    ManualRecoveryConflictError,
    ManualRecoveryNotFoundError,
    ManualRecoveryService,
)


router = APIRouter(prefix="/api/v1/admin/jobs", tags=["admin-recovery"])


@router.post("/{job_id}/retry", response_model=ManualRetryResponse)
async def retry_failed_job(
    job_id: UUID,
    payload: ManualRetryRequest,
    service: Annotated[ManualRecoveryService, Depends(get_manual_recovery_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ManualRetryResponse:
    """로컬 관리자 기능이 켜진 경우에만 실패 작업을 멱등 재처리한다."""
    if not settings.enable_admin_recovery:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    try:
        result = await service.retry(
            job_id,
            idempotency_key=payload.idempotency_key,
            reason=payload.reason,
        )
    except ManualRecoveryNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ManualRecoveryConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return ManualRetryResponse(
        job_id=result.job_id,
        status=result.status,
        workflow_revision=result.workflow_revision,
        idempotent=result.idempotent,
    )
