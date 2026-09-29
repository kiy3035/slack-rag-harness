import asyncio
import re
from typing import Annotated
from uuid import UUID

import aio_pika
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_ingestion_service
from app.api.schemas import (
    AcceptedJobResponse,
    HealthResponse,
    JobResponse,
    LocalEventRequest,
    SlackAckResponse,
    SlackChallengeResponse,
    SlackEnvelope,
)
from app.common.config import Settings, get_settings
from app.common.domain import EventSource
from app.db.session import get_session
from app.integrations.slack.signature import (
    SlackConfigurationError,
    SlackSignatureError,
    SlackSignatureVerifier,
)
from app.services.ingestion import IncomingJob, IngestionService
from app.reviews.api import router as review_router
from app.recovery.api import router as recovery_router


router = APIRouter()
router.include_router(review_router)
router.include_router(recovery_router)


SLACK_MENTION_PREFIX = re.compile(r"^(?:\s*<@[A-Z0-9_]+>)+\s*", re.IGNORECASE)


def normalize_slack_question(text: str) -> str:
    """app_mention을 발생시킨 선두 Bot 멘션을 제거해 실제 질문만 남긴다."""
    return SLACK_MENTION_PREFIX.sub("", text).strip()


@router.get("/health/live", response_model=HealthResponse)
async def health_live() -> HealthResponse:
    """외부 의존성과 무관하게 API 프로세스 생존 여부를 알린다."""
    return HealthResponse(status="ok")


@router.get("/health/ready", response_model=HealthResponse)
async def health_ready(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    """PostgreSQL과 RabbitMQ가 요청 처리 가능한지 실제 연결로 확인한다."""
    try:
        async with asyncio.timeout(2):
            await session.execute(text("SELECT 1"))
            connection = await aio_pika.connect(settings.rabbitmq_url)
            await connection.close()
    except (TimeoutError, OSError, aio_pika.exceptions.AMQPException, SQLAlchemyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DEPENDENCY_NOT_READY",
        ) from exc
    return HealthResponse(status="ok")


@router.post(
    "/api/v1/events",
    response_model=AcceptedJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def accept_local_event(
    payload: LocalEventRequest,
    service: Annotated[IngestionService, Depends(get_ingestion_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AcceptedJobResponse:
    """외부 SaaS 없이 동일 접수 흐름을 검증할 로컬 이벤트를 받는다."""
    if not settings.enable_local_events:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    result = await service.accept(
        IncomingJob(
            source=EventSource.LOCAL,
            external_event_id=payload.external_event_id,
            question=payload.question,
        )
    )
    return AcceptedJobResponse(job_id=result.job_id, duplicate=not result.created)


@router.post(
    "/api/v1/slack/events",
    response_model=SlackAckResponse | SlackChallengeResponse,
    status_code=status.HTTP_200_OK,
)
async def accept_slack_event(
    request: Request,
    service: Annotated[IngestionService, Depends(get_ingestion_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_slack_request_timestamp: Annotated[str | None, Header()] = None,
    x_slack_signature: Annotated[str | None, Header()] = None,
) -> SlackAckResponse | SlackChallengeResponse:
    """Slack 원본 본문을 인증한 뒤 콜백을 빠르게 저장하고 ACK한다."""
    raw_body = await request.body()
    verifier = SlackSignatureVerifier(
        signing_secret=settings.slack_signing_secret.get_secret_value(),
        tolerance_seconds=settings.slack_timestamp_tolerance_seconds,
    )
    try:
        verifier.verify(raw_body, x_slack_request_timestamp, x_slack_signature)
    except SlackConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SLACK_NOT_CONFIGURED",
        ) from exc
    except SlackSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    try:
        envelope = SlackEnvelope.model_validate_json(raw_body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="SLACK_PAYLOAD_INVALID",
        ) from exc

    if envelope.type == "url_verification":
        return SlackChallengeResponse(challenge=envelope.challenge or "")

    event = envelope.event
    if event is None or envelope.event_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="SLACK_PAYLOAD_INVALID",
        )
    if event.bot_id is not None or event.subtype is not None:
        return SlackAckResponse(ignored=True)

    question = normalize_slack_question(event.text)
    if not question:
        return SlackAckResponse(ignored=True)

    result = await service.accept(
        IncomingJob(
            source=EventSource.SLACK,
            external_event_id=envelope.event_id,
            question=question,
            slack_channel_id=event.channel,
            slack_message_ts=event.ts,
            slack_thread_ts=event.thread_ts or event.ts,
        )
    )
    return SlackAckResponse(job_id=result.job_id, duplicate=not result.created)


@router.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: UUID,
    service: Annotated[IngestionService, Depends(get_ingestion_service)],
) -> JobResponse:
    """작업 식별자로 현재 처리 상태를 안전하게 조회한다."""
    job = await service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="JOB_NOT_FOUND")
    return JobResponse.model_validate(job)
