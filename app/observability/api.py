from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.config import Settings, get_settings
from app.db.session import get_session
from app.observability.admin import load_admin_overview, render_admin_page
from app.observability.metrics import refresh_database_metrics, render_metrics


router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """DB 상태 Gauge를 갱신한 뒤 Prometheus Scrape 본문을 반환한다."""
    try:
        await refresh_database_metrics(session)
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="METRICS_DATABASE_UNAVAILABLE",
        ) from error
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_overview(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """명시적으로 활성화된 로컬 환경에서만 최소 관리 화면을 반환한다."""
    if not settings.enable_admin_observability:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    overview = await load_admin_overview(session)
    return HTMLResponse(
        content=render_admin_page(overview),
        headers={"cache-control": "no-store"},
    )
