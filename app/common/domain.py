from enum import StrEnum


class EventSource(StrEnum):
    """외부 이벤트가 유입된 경계를 구분한다."""

    LOCAL = "LOCAL"
    SLACK = "SLACK"


class JobStatus(StrEnum):
    """작업의 복구 가능한 수명 주기 상태를 정의한다."""

    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    RETRIEVING = "RETRIEVING"
    GENERATING = "GENERATING"
    VALIDATING = "VALIDATING"
    RETRY_WAIT = "RETRY_WAIT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"


class OutboxStatus(StrEnum):
    """Outbox 이벤트의 발행 처리 상태를 정의한다."""

    READY = "READY"
    PROCESSING = "PROCESSING"
    SENT = "SENT"
    FAIL = "FAIL"

