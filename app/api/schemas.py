from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.common.domain import EventSource, JobStatus


class LocalEventRequest(BaseModel):
    """Slack 없이 재현하는 로컬 이벤트 입력을 검증한다."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    external_event_id: str = Field(min_length=1, max_length=255)
    question: str = Field(min_length=1, max_length=10_000)


class AcceptedJobResponse(BaseModel):
    """접수 결과에서 작업 식별자와 중복 여부만 공개한다."""

    job_id: UUID
    duplicate: bool


class SlackEvent(BaseModel):
    """처리에 필요한 Slack app_mention 필드만 허용한다."""

    model_config = ConfigDict(extra="allow")

    type: Literal["app_mention"]
    text: str = Field(min_length=1, max_length=10_000)
    channel: str = Field(min_length=1, max_length=100)
    ts: str = Field(min_length=1, max_length=50)
    thread_ts: str | None = Field(default=None, max_length=50)
    bot_id: str | None = Field(default=None, max_length=100)
    subtype: str | None = Field(default=None, max_length=100)


class SlackEnvelope(BaseModel):
    """Slack URL 검증과 이벤트 콜백 Envelope를 하나의 계약으로 검증한다."""

    model_config = ConfigDict(extra="allow")

    type: Literal["url_verification", "event_callback"]
    challenge: str | None = None
    event_id: str | None = Field(default=None, max_length=255)
    event: SlackEvent | None = None

    @model_validator(mode="after")
    def require_type_specific_fields(self) -> Self:
        """Envelope 종류마다 Slack이 요구하는 필드 누락을 거절한다."""
        if self.type == "url_verification" and not self.challenge:
            raise ValueError("url_verification에는 challenge가 필요합니다.")
        if self.type == "event_callback" and (not self.event_id or self.event is None):
            raise ValueError("event_callback에는 event_id와 event가 필요합니다.")
        return self


class SlackChallengeResponse(BaseModel):
    """Slack URL 검증에 challenge를 그대로 돌려준다."""

    challenge: str


class SlackAckResponse(BaseModel):
    """Slack 재시도를 막는 빠른 ACK와 처리 판단을 표현한다."""

    ok: bool = True
    ignored: bool = False
    duplicate: bool = False
    job_id: UUID | None = None


class JobResponse(BaseModel):
    """민감한 내부 정보 없이 현재 작업 상태를 반환한다."""

    model_config = ConfigDict(from_attributes=True)

    job_id: UUID
    source: EventSource
    external_event_id: str
    status: JobStatus
    attempt_count: int
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class HealthResponse(BaseModel):
    """프로세스 또는 의존 서비스의 준비 상태를 반환한다."""

    status: Literal["ok"]

