from dataclasses import dataclass
from datetime import UTC, datetime
import logging

from app.common.domain import OutboxStatus
from app.integrations.slack.client import SlackPublishError, SlackWebClient
from app.integrations.slack.repository import SlackReplyRepository
from app.recovery.errors import RetryPolicy


@dataclass(frozen=True, slots=True)
class SlackPublishCycleResult:
    """단일 Publisher 주기에서 처리한 발신 결과를 요약한다."""

    claimed: bool
    sent: bool
    retry_scheduled: bool
    failed: bool


class SlackReplyPublisher:
    """DB Outbox를 선점해 Slack Thread 답변을 제한 재시도로 발행한다."""

    def __init__(
        self,
        *,
        repository: SlackReplyRepository,
        client: SlackWebClient,
        retry_policy: RetryPolicy,
        lease_seconds: int,
    ) -> None:
        """저장소·Slack Client·재시도 정책과 Lease 기간을 주입한다."""
        self._repository = repository
        self._client = client
        self._retry_policy = retry_policy
        self._lease_seconds = lease_seconds
        self._logger = logging.getLogger(__name__)

    async def process_once(
        self,
        *,
        now: datetime | None = None,
    ) -> SlackPublishCycleResult:
        """발행 가능한 답변 한 건을 처리하고 DB 상태가 확정된 뒤 결과를 반환한다."""
        current_time = now or datetime.now(UTC)
        task = await self._repository.claim_due(
            now=current_time,
            lease_seconds=self._lease_seconds,
        )
        if task is None:
            return SlackPublishCycleResult(False, False, False, False)
        try:
            result = await self._client.post_thread_reply(
                channel_id=task.channel_id,
                thread_ts=task.thread_ts,
                text=task.message_text,
                client_message_id=str(task.reply_id),
            )
        except SlackPublishError as error:
            status = await self._repository.record_failure(
                task,
                error_code=error.error_code,
                transient=error.transient,
                retry_after_seconds=error.retry_after_seconds,
                retry_policy=self._retry_policy,
                now=current_time,
            )
            retry_scheduled = status == OutboxStatus.READY
            self._logger.warning(
                "slack_reply_failed job_id=%s reply_id=%s error_code=%s "
                "attempt_count=%s retry_scheduled=%s",
                task.job_id,
                task.reply_id,
                error.error_code,
                task.attempt_count,
                retry_scheduled,
            )
            return SlackPublishCycleResult(
                True,
                False,
                retry_scheduled,
                not retry_scheduled,
            )
        changed = await self._repository.record_sent(
            task,
            message_ts=result.message_ts,
            now=current_time,
        )
        if not changed:
            raise RuntimeError("SLACK_REPLY_LEASE_LOST")
        self._logger.info(
            "slack_reply_sent job_id=%s reply_id=%s attempt_count=%s",
            task.job_id,
            task.reply_id,
            task.attempt_count,
        )
        return SlackPublishCycleResult(True, True, False, False)
