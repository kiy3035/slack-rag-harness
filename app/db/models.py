from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.domain import EventSource, JobStatus, OutboxStatus
from app.db.base import Base


class AiJob(Base):
    """외부 요청의 상태와 Slack 회신 대상을 영속화한다."""

    __tablename__ = "ai_job"
    __table_args__ = (
        UniqueConstraint("source", "external_event_id", name="uq_ai_job_source_external_event_id"),
    )

    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    source: Mapped[EventSource] = mapped_column(
        Enum(EventSource, name="event_source", native_enum=True, validate_strings=True),
        nullable=False,
    )
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    slack_channel_id: Mapped[str | None] = mapped_column(String(100))
    slack_message_ts: Mapped[str | None] = mapped_column(String(50))
    slack_thread_ts: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", native_enum=True, validate_strings=True),
        nullable=False,
        default=JobStatus.RECEIVED,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_answer: Mapped[str | None] = mapped_column(Text)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    outbox: Mapped["JobOutbox"] = relationship(back_populates="job", uselist=False)


class JobOutbox(Base):
    """작업 저장과 메시지 발행 사이의 유실을 막는 이벤트를 저장한다."""

    __tablename__ = "job_outbox"

    outbox_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("ai_job.job_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status", native_enum=True, validate_strings=True),
        nullable=False,
        default=OutboxStatus.READY,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[AiJob] = relationship(back_populates="outbox")

