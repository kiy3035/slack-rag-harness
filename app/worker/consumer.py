from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
import logging
from uuid import UUID

from aio_pika.abc import AbstractIncomingMessage
from pydantic import ValidationError
from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.domain import JobStatus
from app.db.models import AiJob
from app.messaging.messages import JobMessage
from app.messaging.rabbitmq import RabbitBroker


JobHandler = Callable[[JobMessage], Awaitable[None]]


class JobExecutionGate:
    """최소 한 번 전달되는 메시지를 작업 상태 전이로 한 번만 선점한다."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """메시지마다 독립 트랜잭션을 만들 세션 팩터리를 주입한다."""
        self._session_factory = session_factory

    async def claim(self, job_id: UUID, *, now: datetime) -> bool:
        """RECEIVED 또는 QUEUED 작업만 PROCESSING으로 조건부 전이한다."""
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(AiJob)
                .where(
                    AiJob.job_id == job_id,
                    or_(
                        AiJob.status == JobStatus.RECEIVED,
                        AiJob.status == JobStatus.QUEUED,
                    ),
                )
                .values(
                    status=JobStatus.PROCESSING,
                    attempt_count=AiJob.attempt_count + 1,
                    locked_at=now,
                    started_at=now,
                )
            )
            return result.rowcount == 1

    async def mark_dead_letter(
        self,
        job_id: UUID,
        *,
        now: datetime,
        error_code: str,
    ) -> bool:
        """선점된 작업의 영구 처리 실패를 검색 가능한 상태로 남긴다."""
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(AiJob)
                .where(
                    AiJob.job_id == job_id,
                    AiJob.status == JobStatus.PROCESSING,
                )
                .values(
                    status=JobStatus.DEAD_LETTER,
                    failure_code=error_code,
                    failure_message="Worker 처리기가 메시지를 완료하지 못했습니다.",
                    locked_at=None,
                    completed_at=now,
                )
            )
            return result.rowcount == 1


class RabbitJobConsumer:
    """RabbitMQ 메시지를 검증하고 조건부 작업 선점 후 Handler에 전달한다."""

    def __init__(
        self,
        broker: RabbitBroker,
        gate: JobExecutionGate,
        handler: JobHandler,
    ) -> None:
        """Broker·DB 실행 Gate·교체 가능한 작업 Handler를 주입한다."""
        self._broker = broker
        self._gate = gate
        self._handler = handler
        self._logger = logging.getLogger(__name__)

    async def consume_one(self, *, timeout: float = 5.0) -> bool:
        """통합 검증을 위해 Queue에서 메시지 하나를 가져와 처리한다."""
        delivery = await self._broker.topology.job_queue.get(timeout=timeout, fail=False)
        if delivery is None:
            return False
        await self._handle_delivery(delivery)
        return True

    async def _handle_delivery(self, delivery: AbstractIncomingMessage) -> None:
        """검증 실패·중복·Handler 실패별 ACK 또는 DLQ 처리를 결정한다."""
        try:
            message = JobMessage.model_validate_json(delivery.body)
        except ValidationError:
            await self._broker.publish_dead_letter(
                delivery.body,
                message_id=delivery.message_id,
                correlation_id=delivery.correlation_id,
                error_code="MESSAGE_SCHEMA_INVALID",
            )
            await delivery.ack()
            return

        claimed = await self._gate.claim(message.job_id, now=datetime.now(UTC))
        if not claimed:
            await delivery.ack()
            self._logger.info(
                "job_message_duplicate job_id=%s request_id=%s thread_id=%s",
                message.job_id,
                message.request_id,
                message.thread_id,
            )
            return
        try:
            await self._handler(message)
        except Exception as error:
            error_code = f"WORKER_{type(error).__name__.upper()}"
            await self._gate.mark_dead_letter(
                message.job_id,
                now=datetime.now(UTC),
                error_code=error_code,
            )
            await self._broker.publish_dead_letter(
                delivery.body,
                message_id=delivery.message_id,
                correlation_id=delivery.correlation_id,
                error_code=error_code,
            )
            await delivery.ack()
            self._logger.error(
                "job_message_dead_letter job_id=%s request_id=%s thread_id=%s error_code=%s",
                message.job_id,
                message.request_id,
                message.thread_id,
                error_code,
            )
            return
        await delivery.ack()
