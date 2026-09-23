from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.recovery.service import ManualRecoveryService
from app.reviews.service import ReviewService
from app.services.ingestion import IngestionService, JobRepository


def get_ingestion_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IngestionService:
    """요청 세션으로 트랜잭션 가능한 작업 접수 서비스를 구성한다."""
    return IngestionService(JobRepository(session))


def get_review_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReviewService:
    """요청 세션으로 원자적 검토 결정 서비스를 구성한다."""
    return ReviewService(session)


def get_manual_recovery_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ManualRecoveryService:
    """요청 세션으로 원자적 수동 재처리 서비스를 구성한다."""
    return ManualRecoveryService(session)
