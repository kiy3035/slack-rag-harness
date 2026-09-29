from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.domain import EventSource, OutboxStatus
from app.db.models import AiJob, SlackReplyOutbox
from app.recovery.errors import RetryPolicy


@dataclass(frozen=True, slots=True)
class SlackReplyTask:
    """DB Lease로 선점한 Slack 답변 발신에 필요한 값만 전달한다."""

    reply_id: UUID
    job_id: UUID
    channel_id: str
    thread_ts: str
    message_text: str
    attempt_count: int
    locked_at: datetime


async def enqueue_slack_reply(
    session: AsyncSession,
    *,
    job_id: UUID,
    answer: str,
) -> bool:
    """Slack 작업의 완료 답변만 같은 트랜잭션에서 한 번 발신 예약한다."""
    target = (
        await session.execute(
            select(
                AiJob.source,
                AiJob.slack_channel_id,
                AiJob.slack_thread_ts,
            ).where(AiJob.job_id == job_id)
        )
    ).one_or_none()
    if target is None:
        raise LookupError("SLACK_REPLY_JOB_NOT_FOUND")
    source, channel_id, thread_ts = target
    if source != EventSource.SLACK:
        return False
    if not channel_id or not thread_ts:
        raise ValueError("Slack 작업의 회신 대상이 누락됐습니다.")
    statement = (
        insert(SlackReplyOutbox)
        .values(
            reply_id=uuid4(),
            job_id=job_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_text=answer,
            status=OutboxStatus.READY,
            attempt_count=0,
        )
        .on_conflict_do_nothing(index_elements=["job_id"])
    )
    result = await session.execute(statement)
    return result.rowcount == 1


class SlackReplyRepository:
    """Slack 답변 Outbox의 선점과 성공·실패 상태를 짧은 트랜잭션으로 관리한다."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """각 상태 변경에 독립 Session을 만들 팩터리를 보관한다."""
        self._session_factory = session_factory

    async def claim_due(
        self,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> SlackReplyTask | None:
        """발행 시각이 됐거나 Lease가 만료된 답변 한 건을 원자적으로 선점한다."""
        stale_before = now - timedelta(seconds=lease_seconds)
        eligible = or_(
            and_(
                SlackReplyOutbox.status == OutboxStatus.READY,
                or_(
                    SlackReplyOutbox.next_retry_at.is_(None),
                    SlackReplyOutbox.next_retry_at <= now,
                ),
            ),
            and_(
                SlackReplyOutbox.status == OutboxStatus.PROCESSING,
                or_(
                    SlackReplyOutbox.locked_at.is_(None),
                    SlackReplyOutbox.locked_at <= stale_before,
                ),
            ),
        )
        async with self._session_factory() as session, session.begin():
            reply = await session.scalar(
                select(SlackReplyOutbox)
                .where(eligible)
                .order_by(SlackReplyOutbox.created_at, SlackReplyOutbox.reply_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if reply is None:
                return None
            reply.status = OutboxStatus.PROCESSING
            reply.attempt_count += 1
            reply.locked_at = now
            reply.next_retry_at = None
            await session.flush()
            return SlackReplyTask(
                reply_id=reply.reply_id,
                job_id=reply.job_id,
                channel_id=reply.channel_id,
                thread_ts=reply.thread_ts,
                message_text=reply.message_text,
                attempt_count=reply.attempt_count,
                locked_at=now,
            )

    async def record_sent(
        self,
        task: SlackReplyTask,
        *,
        message_ts: str,
        now: datetime,
    ) -> bool:
        """현재 Lease가 유지된 발신만 Slack Timestamp와 함께 완료 처리한다."""
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(SlackReplyOutbox)
                .where(
                    SlackReplyOutbox.reply_id == task.reply_id,
                    SlackReplyOutbox.status == OutboxStatus.PROCESSING,
                    SlackReplyOutbox.locked_at == task.locked_at,
                )
                .values(
                    status=OutboxStatus.SENT,
                    last_error=None,
                    next_retry_at=None,
                    locked_at=None,
                    sent_at=now,
                    slack_message_ts=message_ts,
                )
            )
            return result.rowcount == 1

    async def record_failure(
        self,
        task: SlackReplyTask,
        *,
        error_code: str,
        transient: bool,
        retry_after_seconds: int | None,
        retry_policy: RetryPolicy,
        now: datetime,
    ) -> OutboxStatus:
        """일시 오류만 제한 Backoff로 다시 열고 영구·소진 오류는 FAIL로 고정한다."""
        should_retry = transient and task.attempt_count < retry_policy.max_attempts
        status = OutboxStatus.READY if should_retry else OutboxStatus.FAIL
        next_retry_at = None
        if should_retry:
            delay_seconds = retry_policy.delay_seconds(task.attempt_count)
            if retry_after_seconds is not None:
                delay_seconds = max(delay_seconds, retry_after_seconds)
            next_retry_at = now + timedelta(seconds=delay_seconds)
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(SlackReplyOutbox)
                .where(
                    SlackReplyOutbox.reply_id == task.reply_id,
                    SlackReplyOutbox.status == OutboxStatus.PROCESSING,
                    SlackReplyOutbox.locked_at == task.locked_at,
                )
                .values(
                    status=status,
                    last_error=error_code[:100],
                    next_retry_at=next_retry_at,
                    locked_at=None,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError("SLACK_REPLY_LEASE_LOST")
        return status
