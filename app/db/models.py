from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Float,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import UserDefinedType

from app.common.domain import EventSource, JobStatus, OutboxStatus, ReviewStatus
from app.db.base import Base


class VectorType(UserDefinedType[str]):
    """pgvector 열의 차원을 SQLAlchemy 메타데이터에 표현한다."""

    cache_ok = True

    def __init__(self, dimensions: int) -> None:
        """고정 차원 벡터 열을 만들 수 있도록 차원을 보관한다."""
        self.dimensions = dimensions

    def get_col_spec(self, **_: object) -> str:
        """PostgreSQL이 이해하는 vector 타입 선언을 반환한다."""
        return f"vector({self.dimensions})"


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
    workflow_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    dlq_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dlq_last_error: Mapped[str | None] = mapped_column(String(100))
    dlq_next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dlq_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    outbox: Mapped["JobOutbox"] = relationship(back_populates="job", uselist=False)
    reviews: Mapped[list["ReviewQueue"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    citations: Mapped[list["AnswerCitation"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    recovery_requests: Mapped[list["JobRecoveryRequest"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    slack_reply: Mapped["SlackReplyOutbox | None"] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )


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


class SlackReplyOutbox(Base):
    """완료 답변의 Slack Thread 발신 상태를 복구 가능한 원장으로 저장한다."""

    __tablename__ = "slack_reply_outbox"

    reply_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("ai_job.job_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    channel_id: Mapped[str] = mapped_column(String(100), nullable=False)
    thread_ts: Mapped[str] = mapped_column(String(50), nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status", native_enum=True, validate_strings=True),
        nullable=False,
        default=OutboxStatus.READY,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(100))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slack_message_ts: Mapped[str | None] = mapped_column(String(50))

    job: Mapped[AiJob] = relationship(back_populates="slack_reply")


class JobRecoveryRequest(Base):
    """관리자 수동 재처리 요청과 멱등 키를 감사 가능한 원장으로 저장한다."""

    __tablename__ = "job_recovery_request"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "idempotency_key",
            name="uq_job_recovery_request_job_key",
        ),
    )

    recovery_request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("ai_job.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    from_status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", native_enum=True, validate_strings=True),
        nullable=False,
    )
    resulting_workflow_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    job: Mapped[AiJob] = relationship(back_populates="recovery_requests")


class KnowledgeDocument(Base):
    """현재 사용 중인 운영 문서 버전과 원문 식별자를 저장한다."""

    __tablename__ = "knowledge_document"
    __table_args__ = (
        CheckConstraint("current_version >= 1", name="ck_knowledge_document_version_positive"),
    )

    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_path: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class KnowledgeChunk(Base):
    """문서 검색에 사용하는 분할 본문과 임베딩을 저장한다."""

    __tablename__ = "knowledge_chunk"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "document_version",
            "chunk_index",
            name="uq_knowledge_chunk_document_version_index",
        ),
        CheckConstraint("document_version >= 1", name="ck_knowledge_chunk_version_positive"),
        CheckConstraint("chunk_index >= 0", name="ck_knowledge_chunk_index_nonnegative"),
    )

    chunk_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_document.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[str] = mapped_column(VectorType(768), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")


class ReviewQueue(Base):
    """자동 처리가 중단된 작업과 사람의 단일 결정을 영속화한다."""

    __tablename__ = "review_queue"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "workflow_revision",
            name="uq_review_queue_job_revision",
        ),
    )

    review_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("ai_job.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    draft_answer: Mapped[str | None] = mapped_column(Text)
    draft_citations: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    allowed_citations: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status", native_enum=True, validate_strings=True),
        nullable=False,
        default=ReviewStatus.WAITING,
    )
    review_comment: Mapped[str | None] = mapped_column(String(1_000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[AiJob] = relationship(back_populates="reviews")


class AnswerCitation(Base):
    """최종 답변과 검증된 검색 Chunk의 연결 및 검색 점수를 저장한다."""

    __tablename__ = "answer_citation"
    __table_args__ = (
        UniqueConstraint("job_id", "chunk_id", name="uq_answer_citation_job_chunk"),
        CheckConstraint(
            "similarity_score >= -1 AND similarity_score <= 1",
            name="ck_answer_citation_similarity_range",
        ),
    )

    citation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("ai_job.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_document.document_id", ondelete="RESTRICT"),
        nullable=False,
    )
    chunk_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_chunk.chunk_id", ondelete="RESTRICT"),
        nullable=False,
    )
    similarity_score: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    job: Mapped[AiJob] = relationship(back_populates="citations")
