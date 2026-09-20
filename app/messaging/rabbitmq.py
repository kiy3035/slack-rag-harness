from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractQueue,
    AbstractRobustConnection,
)

from app.common.config import Settings
from app.messaging.messages import JobMessage


@dataclass(frozen=True, slots=True)
class DeclaredTopology:
    """선언 완료된 Exchange와 Queue 참조를 한데 묶는다."""

    job_exchange: AbstractExchange
    retry_exchange: AbstractExchange
    dead_letter_exchange: AbstractExchange
    job_queue: AbstractQueue
    retry_queue: AbstractQueue
    dead_letter_queue: AbstractQueue


class RabbitBroker:
    """확인 가능한 영속 메시지 발행과 Queue 선언을 담당한다."""

    def __init__(self, settings: Settings) -> None:
        """검증된 RabbitMQ 연결과 Queue 설정을 보관한다."""
        self._settings = settings
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._topology: DeclaredTopology | None = None
        self._connection_lock = asyncio.Lock()
        self._logger = logging.getLogger(__name__)

    @property
    def topology(self) -> DeclaredTopology:
        """연결 후 선언된 Queue 참조를 반환한다."""
        if self._topology is None:
            raise RuntimeError("RABBITMQ_TOPOLOGY_NOT_DECLARED")
        return self._topology

    async def connect(self) -> None:
        """Publisher Confirm 채널을 열고 모든 영속 Queue를 선언한다."""
        async with self._connection_lock:
            await self._discard_connection()
            await self._open_connection()

    async def _open_connection(self) -> None:
        """새 RabbitMQ 연결과 채널에 영속 Topology를 구성한다."""
        self._connection = await aio_pika.connect_robust(self._settings.rabbitmq_url)
        self._channel = await self._connection.channel(
            publisher_confirms=True,
            on_return_raises=True,
        )
        await self._channel.set_qos(prefetch_count=1)
        self._topology = await self._declare_topology(self._channel)

    async def close(self) -> None:
        """열린 RabbitMQ 연결을 안전하게 닫는다."""
        async with self._connection_lock:
            await self._discard_connection()

    async def publish_job(self, message: JobMessage) -> None:
        """확인 가능한 영속 메시지를 기본 작업 Queue로 발행한다."""
        await self._ensure_connected()
        try:
            await self.topology.job_exchange.publish(
                self._build_message(message),
                routing_key=self._settings.rabbitmq_job_routing_key,
                mandatory=True,
            )
        except Exception:
            await self._invalidate_connection()
            raise

    async def publish_dead_letter(
        self,
        body: bytes,
        *,
        message_id: str | None,
        correlation_id: str | None,
        error_code: str,
    ) -> None:
        """처리 불가능한 원문을 오류 코드와 함께 DLQ로 격리한다."""
        await self._ensure_connected()
        message = aio_pika.Message(
            body=body,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type="application/json",
            message_id=message_id,
            correlation_id=correlation_id,
            headers={"x-error-code": error_code},
        )
        try:
            await self.topology.dead_letter_exchange.publish(
                message,
                routing_key=self._settings.rabbitmq_dead_letter_routing_key,
                mandatory=True,
            )
        except Exception:
            await self._invalidate_connection()
            raise

    async def delete_topology(self) -> None:
        """격리된 통합 테스트가 만든 Queue와 Exchange를 제거한다."""
        await self._ensure_connected()
        topology = self.topology
        await topology.job_queue.delete(if_unused=False, if_empty=False)
        await topology.retry_queue.delete(if_unused=False, if_empty=False)
        await topology.dead_letter_queue.delete(if_unused=False, if_empty=False)
        await topology.job_exchange.delete(if_unused=False)
        await topology.retry_exchange.delete(if_unused=False)
        await topology.dead_letter_exchange.delete(if_unused=False)

    async def _ensure_connected(self) -> None:
        """닫힌 Channel을 발견하면 새 연결과 Topology로 교체한다."""
        if self._is_ready():
            return
        async with self._connection_lock:
            if self._is_ready():
                return
            await self._discard_connection()
            await self._open_connection()

    async def _invalidate_connection(self) -> None:
        """발행 오류가 난 연결을 다음 시도에서 재사용하지 않도록 폐기한다."""
        async with self._connection_lock:
            await self._discard_connection()

    async def _discard_connection(self) -> None:
        """현재 연결을 정리하고 모든 Channel 참조를 초기화한다."""
        connection = self._connection
        self._topology = None
        self._channel = None
        self._connection = None
        if connection is None or connection.is_closed:
            return
        try:
            await connection.close()
        except Exception as error:
            self._logger.warning(
                "rabbitmq_connection_close_failed error_code=%s",
                type(error).__name__,
            )

    def _is_ready(self) -> bool:
        """연결·Channel·Topology가 모두 발행 가능한 상태인지 판정한다."""
        return (
            self._connection is not None
            and not self._connection.is_closed
            and self._channel is not None
            and not self._channel.is_closed
            and self._topology is not None
        )

    async def _declare_topology(self, channel: AbstractChannel) -> DeclaredTopology:
        """작업·지연 재시도·DLQ를 고정 Routing 규칙으로 선언한다."""
        job_exchange = await channel.declare_exchange(
            self._settings.rabbitmq_job_exchange,
            aio_pika.ExchangeType.DIRECT,
            durable=True,
        )
        retry_exchange = await channel.declare_exchange(
            self._settings.rabbitmq_retry_exchange,
            aio_pika.ExchangeType.DIRECT,
            durable=True,
        )
        dead_letter_exchange = await channel.declare_exchange(
            self._settings.rabbitmq_dead_letter_exchange,
            aio_pika.ExchangeType.DIRECT,
            durable=True,
        )
        job_queue = await channel.declare_queue(
            self._settings.rabbitmq_job_queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": self._settings.rabbitmq_retry_exchange,
                "x-dead-letter-routing-key": self._settings.rabbitmq_retry_routing_key,
            },
        )
        retry_queue = await channel.declare_queue(
            self._settings.rabbitmq_retry_queue,
            durable=True,
            arguments={
                "x-message-ttl": self._settings.rabbitmq_retry_delay_ms,
                "x-dead-letter-exchange": self._settings.rabbitmq_job_exchange,
                "x-dead-letter-routing-key": self._settings.rabbitmq_job_routing_key,
            },
        )
        dead_letter_queue = await channel.declare_queue(
            self._settings.rabbitmq_dead_letter_queue,
            durable=True,
        )
        await job_queue.bind(
            job_exchange,
            routing_key=self._settings.rabbitmq_job_routing_key,
        )
        await retry_queue.bind(
            retry_exchange,
            routing_key=self._settings.rabbitmq_retry_routing_key,
        )
        await dead_letter_queue.bind(
            dead_letter_exchange,
            routing_key=self._settings.rabbitmq_dead_letter_routing_key,
        )
        return DeclaredTopology(
            job_exchange=job_exchange,
            retry_exchange=retry_exchange,
            dead_letter_exchange=dead_letter_exchange,
            job_queue=job_queue,
            retry_queue=retry_queue,
            dead_letter_queue=dead_letter_queue,
        )

    def _build_message(self, message: JobMessage) -> aio_pika.Message:
        """스키마 검증된 작업을 추적 가능한 영속 AMQP 메시지로 변환한다."""
        headers: dict[str, Any] = {"x-schema-version": message.schema_version}
        if message.thread_id is not None:
            headers["x-thread-id"] = message.thread_id
        return aio_pika.Message(
            body=message.model_dump_json().encode("utf-8"),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type="application/json",
            message_id=str(message.outbox_id),
            correlation_id=str(message.job_id),
            headers=headers,
        )
