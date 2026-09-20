from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.common.config import Settings
from app.messaging.messages import JobMessage
from app.outbox.publisher import OutboxPublisher
from app.outbox.repository import ClaimedOutbox, FailureResult


FIXED_NOW = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)


class RecordingRepository:
    """Publisher 실패 상태 전이를 메모리에 기록하는 저장소 대역이다."""

    def __init__(self, claim: ClaimedOutbox) -> None:
        """한 번 반환할 선점 결과와 기록 필드를 초기화한다."""
        self.claim = claim
        self.failure_arguments: dict[str, object] | None = None

    async def recover_stale(self, **_: object) -> tuple[int, int]:
        """단위 테스트에서는 복구 대상이 없다고 반환한다."""
        return 0, 0

    async def claim_ready(self, **_: object) -> list[ClaimedOutbox]:
        """준비된 고정 Outbox 한 건을 반환한다."""
        return [self.claim]

    async def mark_sent(self, claim: ClaimedOutbox, *, now: datetime) -> bool:
        """실패 발행 경로에서 성공 처리가 호출되면 테스트를 실패시킨다."""
        raise AssertionError(f"예상하지 않은 성공 처리: {claim.outbox_id} {now}")

    async def record_failure(self, claim: ClaimedOutbox, **arguments: object) -> FailureResult:
        """재시도 시각과 정제된 오류를 검증할 수 있도록 기록한다."""
        self.failure_arguments = {"claim": claim, **arguments}
        return FailureResult(updated=True, exhausted=False)


class FailingBroker:
    """RabbitMQ 연결 실패를 결정적으로 재현하는 Broker 대역이다."""

    async def publish_job(self, _: JobMessage) -> None:
        """모든 발행에서 연결 오류를 발생시킨다."""
        raise ConnectionError("민감한 연결 문자열을 포함하지 않는다")


def fixed_clock() -> datetime:
    """Backoff 계산 검증에 사용할 고정 UTC 시각을 반환한다."""
    return FIXED_NOW


@pytest.mark.asyncio
async def test_publish_failure_records_bounded_backoff_without_sensitive_message() -> None:
    """발행 실패가 제한된 재시도 시각과 오류 유형만 저장하는지 검증한다."""
    claim = ClaimedOutbox(
        outbox_id=uuid4(),
        job_id=uuid4(),
        request_id="request-1",
        thread_id="thread-1",
        attempt_count=2,
        locked_at=FIXED_NOW,
    )
    repository = RecordingRepository(claim)
    settings = Settings(
        outbox_retry_base_seconds=2,
        outbox_retry_max_seconds=5,
        outbox_max_attempts=5,
    )
    publisher = OutboxPublisher(repository, FailingBroker(), settings, clock=fixed_clock)

    result = await publisher.process_once()

    assert result.claimed == 1
    assert result.failed == 1
    assert result.sent == 0
    assert repository.failure_arguments is not None
    assert repository.failure_arguments["error"] == "ConnectionError"
    assert repository.failure_arguments["next_retry_at"] == datetime(
        2026,
        9,
        20,
        0,
        0,
        5,
        tzinfo=UTC,
    )

