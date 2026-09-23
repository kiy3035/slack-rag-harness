from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
import logging
from uuid import UUID

from aio_pika.abc import AbstractIncomingMessage
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.messaging.messages import JobMessage
from app.messaging.rabbitmq import RabbitBroker
from app.recovery.errors import ErrorClassifier, FailureDecision, RetryPolicy
from app.recovery.repository import FailureTransition, JobRecoveryRepository


JobHandler = Callable[[JobMessage], Awaitable[None]]


class JobExecutionGate:
    """최소 한 번 전달되는 메시지를 작업 상태 전이로 한 번만 선점한다."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        """복구 저장소와 Worker 실행 재시도 상한을 구성한다."""
        self._repository = JobRecoveryRepository(session_factory)
        self._retry_policy = retry_policy or RetryPolicy(
            max_attempts=3,
            base_seconds=5,
            max_seconds=300,
        )

    async def claim(self, job_id: UUID, *, now: datetime) -> bool:
        """RECEIVED 또는 QUEUED 작업만 PROCESSING으로 조건부 전이한다."""
        return await self._repository.claim(job_id, now=now)

    async def record_failure(
        self,
        job_id: UUID,
        *,
        now: datetime,
        decision: FailureDecision,
    ) -> FailureTransition:
        """선점된 작업 오류를 정책에 따라 검토·재시도·DLQ 상태로 기록한다."""
        return await self._repository.record_failure(
            job_id,
            decision=decision,
            policy=self._retry_policy,
            now=now,
        )


class RabbitJobConsumer:
    """RabbitMQ 메시지를 검증하고 조건부 작업 선점 후 Handler에 전달한다."""

    def __init__(
        self,
        broker: RabbitBroker,
        gate: JobExecutionGate,
        handler: JobHandler,
        error_classifier: ErrorClassifier | None = None,
    ) -> None:
        """Broker·DB 실행 Gate·작업 Handler·오류 분류기를 주입한다."""
        self._broker = broker
        self._gate = gate
        self._handler = handler
        self._error_classifier = error_classifier or ErrorClassifier()
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
            decision = self._error_classifier.classify(error)
            transition = await self._gate.record_failure(
                message.job_id, now=datetime.now(UTC), decision=decision
            )
            await delivery.ack()
            self._logger.warning(
                "job_message_failed job_id=%s request_id=%s thread_id=%s "
                "failure_kind=%s error_code=%s status=%s",
                message.job_id,
                message.request_id,
                message.thread_id,
                decision.kind,
                transition.error_code,
                transition.status,
            )
            return
        await delivery.ack()
