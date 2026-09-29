import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging

from app.common.config import Settings
from app.messaging.messages import JobMessage
from app.messaging.rabbitmq import RabbitBroker
from app.observability.metrics import record_outbox_cycle
from app.outbox.repository import OutboxRepository


Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class PublishCycleResult:
    """단일 Publisher 주기의 선점·성공·실패 건수를 기록한다."""

    claimed: int
    sent: int
    failed: int
    exhausted: int
    recovered: int


class OutboxPublisher:
    """DB Outbox를 짧게 선점하고 트랜잭션 밖에서 RabbitMQ로 발행한다."""

    def __init__(
        self,
        repository: OutboxRepository,
        broker: RabbitBroker,
        settings: Settings,
        clock: Clock | None = None,
    ) -> None:
        """저장소·Broker·시간 경계를 주입해 재현 가능한 발행기를 만든다."""
        self._repository = repository
        self._broker = broker
        self._settings = settings
        self._clock = clock or self._utc_now
        self._logger = logging.getLogger(__name__)

    async def process_once(self) -> PublishCycleResult:
        """만료 Lease를 복구한 뒤 READY Outbox 한 묶음을 발행한다."""
        now = self._clock()
        recovered, lease_exhausted = await self._repository.recover_stale(
            now=now,
            lease_timeout=timedelta(seconds=self._settings.outbox_lease_seconds),
            max_attempts=self._settings.outbox_max_attempts,
        )
        claims = await self._repository.claim_ready(
            now=now,
            limit=self._settings.outbox_batch_size,
        )
        sent = 0
        failed = 0
        exhausted = lease_exhausted
        for claim in claims:
            message = JobMessage(
                outbox_id=claim.outbox_id,
                job_id=claim.job_id,
                request_id=claim.request_id,
                thread_id=claim.thread_id,
            )
            try:
                await self._broker.publish_job(message)
            except Exception as error:
                failed += 1
                retry_at = now + timedelta(seconds=self._backoff_seconds(claim.attempt_count))
                failure = await self._repository.record_failure(
                    claim,
                    now=now,
                    next_retry_at=retry_at,
                    error=type(error).__name__,
                    max_attempts=self._settings.outbox_max_attempts,
                )
                if failure.exhausted:
                    exhausted += 1
                self._logger.warning(
                    "outbox_publish_failed outbox_id=%s job_id=%s request_id=%s thread_id=%s error_code=%s",
                    claim.outbox_id,
                    claim.job_id,
                    claim.request_id,
                    claim.thread_id,
                    type(error).__name__,
                )
                continue
            if await self._repository.mark_sent(claim, now=self._clock()):
                sent += 1
                self._logger.info(
                    "outbox_publish_sent outbox_id=%s job_id=%s request_id=%s thread_id=%s",
                    claim.outbox_id,
                    claim.job_id,
                    claim.request_id,
                    claim.thread_id,
                )
        result = PublishCycleResult(
            claimed=len(claims),
            sent=sent,
            failed=failed,
            exhausted=exhausted,
            recovered=recovered,
        )
        record_outbox_cycle(
            sent=result.sent,
            failed=result.failed,
            exhausted=result.exhausted,
            recovered=result.recovered,
        )
        return result

    async def run(self, stop_event: asyncio.Event) -> None:
        """중단 신호까지 제한된 간격으로 발행 주기를 반복한다."""
        while not stop_event.is_set():
            await self.process_once()
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._settings.outbox_poll_interval_seconds,
                )
            except TimeoutError:
                continue

    def _backoff_seconds(self, attempt_count: int) -> int:
        """설정된 상한 안에서 발행 실패의 지수 Backoff를 계산한다."""
        delay = self._settings.outbox_retry_base_seconds * (2**attempt_count)
        return min(delay, self._settings.outbox_retry_max_seconds)

    def _utc_now(self) -> datetime:
        """저장과 비교에 사용할 timezone-aware UTC 현재 시각을 반환한다."""
        return datetime.now(UTC)

