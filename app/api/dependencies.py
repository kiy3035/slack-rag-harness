from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.ingestion import IngestionService, JobRepository


def get_ingestion_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IngestionService:
    """요청 세션으로 트랜잭션 가능한 작업 접수 서비스를 구성한다."""
    return IngestionService(JobRepository(session))

