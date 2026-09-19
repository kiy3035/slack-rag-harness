"""작업 접수와 원자적 Outbox 테이블을 생성한다.

Revision ID: 0001
Revises: None
Create Date: 2026-09-19
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


event_source = postgresql.ENUM("LOCAL", "SLACK", name="event_source", create_type=False)
job_status = postgresql.ENUM(
    "RECEIVED",
    "QUEUED",
    "PROCESSING",
    "RETRIEVING",
    "GENERATING",
    "VALIDATING",
    "RETRY_WAIT",
    "REVIEW_REQUIRED",
    "COMPLETED",
    "REJECTED",
    "FAILED",
    "DEAD_LETTER",
    name="job_status",
    create_type=False,
)
outbox_status = postgresql.ENUM(
    "READY",
    "PROCESSING",
    "SENT",
    "FAIL",
    name="outbox_status",
    create_type=False,
)


def upgrade() -> None:
    """pgvector 확장과 1단계 작업·Outbox 스키마를 원자적으로 추가한다."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    event_source.create(op.get_bind(), checkfirst=True)
    job_status.create(op.get_bind(), checkfirst=True)
    outbox_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "ai_job",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", event_source, nullable=False),
        sa.Column("external_event_id", sa.String(length=255), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("slack_channel_id", sa.String(length=100), nullable=True),
        sa.Column("slack_message_ts", sa.String(length=50), nullable=True),
        sa.Column("slack_thread_ts", sa.String(length=50), nullable=True),
        sa.Column("status", job_status, nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_answer", sa.Text(), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_message", sa.String(length=500), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("job_id", name="pk_ai_job"),
        sa.UniqueConstraint(
            "source",
            "external_event_id",
            name="uq_ai_job_source_external_event_id",
        ),
    )
    op.create_table(
        "job_outbox",
        sa.Column("outbox_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", outbox_status, nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["ai_job.job_id"],
            name="fk_job_outbox_job_id_ai_job",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("outbox_id", name="pk_job_outbox"),
        sa.UniqueConstraint("job_id", name="uq_job_outbox_job_id"),
    )


def downgrade() -> None:
    """1단계 테이블과 전용 Enum 타입을 역순으로 제거한다."""
    op.drop_table("job_outbox")
    op.drop_table("ai_job")
    outbox_status.drop(op.get_bind(), checkfirst=True)
    job_status.drop(op.get_bind(), checkfirst=True)
    event_source.drop(op.get_bind(), checkfirst=True)
