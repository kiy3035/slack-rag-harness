from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging

from app.common.config import Settings
from app.messaging.messages import JobMessage
from app.messaging.rabbitmq import RabbitBroker
from app.observability.metrics import record_recovery_cycle
from app.recovery.errors import RetryPolicy
from app.recovery.repository import JobRecoveryRepository


Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class RecoveryCycleResult:
    """단일 복구 주기의 재발행·Lease·DLQ 처리 건수를 기록한다."""

    retries_released: int
    stale_retried: int
    stale_exhausted: int
    dlq_claimed: int
    dlq_published: int
    dlq_failed: int


class RecoveryScheduler:
    """DB를 기준으로 Worker 재시도와 미발행 DLQ를 주기적으로 복구한다."""

    def __init__(
        self,
        repository: JobRecoveryRepository,
        broker: RabbitBroker,
        settings: Settings,
        clock: Clock | None = None,
    ) -> None:
        """복구 저장소·Broker·제한 설정과 교체 가능한 시계를 주입한다."""
        self._repository = repository
        self._broker = broker
        self._settings = settings
        self._clock = clock or self._utc_now
        self._policy = RetryPolicy(
            max_attempts=settings.worker_max_attempts,
            base_seconds=settings.worker_retry_base_seconds,
            max_seconds=settings.worker_retry_max_seconds,
        )
        self._logger = logging.getLogger(__name__)

    async def process_once(self) -> RecoveryCycleResult:
        """만료 작업을 복구하고 재시도 Outbox와 DLQ 발행을 한 번 처리한다."""
        now = self._clock()
        stale = await self._repository.recover_stale_processing(
            now=now,
            lease_timeout=timedelta(
                seconds=self._settings.worker_processing_lease_seconds
            ),
            policy=self._policy,
            limit=self._settings.worker_recovery_batch_size,
        )
        retries_released = await self._repository.release_due_retries(
            now=now,
            limit=self._settings.worker_recovery_batch_size,
        )
        claims = await self._repository.claim_pending_dead_letters(
            now=now,
            lease_timeout=timedelta(
                seconds=self._settings.worker_processing_lease_seconds
            ),
            limit=self._settings.worker_recovery_batch_size,
        )
        published = 0
        failed = 0
        for claim in claims:
            message = JobMessage(
                outbox_id=claim.outbox_id,
                job_id=claim.job_id,
                request_id=claim.request_id,
                thread_id=claim.thread_id,
            )
            try:
                await self._broker.publish_dead_letter(
                    message.model_dump_json().encode("utf-8"),
                    message_id=str(claim.outbox_id),
                    correlation_id=str(claim.job_id),
                    error_code=claim.error_code,
                )
            except Exception as error:
                failed += 1
                delay = self._dlq_backoff_seconds(claim.attempt_count)
                await self._repository.record_dead_letter_publish_failure(
                    claim,
                    now=now,
                    next_retry_at=now + timedelta(seconds=delay),
                    error_code=type(error).__name__,
                )
                self._logger.warning(
                    "dead_letter_publish_failed job_id=%s request_id=%s "
                    "thread_id=%s error_code=%s",
                    claim.job_id,
                    claim.request_id,
                    claim.thread_id,
                    type(error).__name__,
                )
                continue
            if await self._repository.mark_dead_letter_published(
                claim, now=self._clock()
            ):
                published += 1
                self._logger.info(
                    "dead_letter_published job_id=%s request_id=%s "
                    "thread_id=%s error_code=%s",
                    claim.job_id,
                    claim.request_id,
                    claim.thread_id,
                    claim.error_code,
                )
        result = RecoveryCycleResult(
            retries_released=retries_released,
            stale_retried=stale.retried,
            stale_exhausted=stale.exhausted,
            dlq_claimed=len(claims),
            dlq_published=published,
            dlq_failed=failed,
        )
        record_recovery_cycle(
            retries_released=result.retries_released,
            stale_retried=result.stale_retried,
            stale_exhausted=result.stale_exhausted,
            dlq_published=result.dlq_published,
            dlq_failed=result.dlq_failed,
        )
        return result

    def _dlq_backoff_seconds(self, attempt_count: int) -> int:
        """DLQ 발행 실패가 RabbitMQ를 압박하지 않도록 지수 Backoff를 계산한다."""
        delay = self._settings.dlq_publish_retry_base_seconds * (2**attempt_count)
        return min(delay, self._settings.dlq_publish_retry_max_seconds)

    def _utc_now(self) -> datetime:
        """비교와 영속화에 사용할 timezone-aware UTC 현재 시각을 반환한다."""
        return datetime.now(UTC)
