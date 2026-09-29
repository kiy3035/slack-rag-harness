"""Slack Thread 답변 발신 원장을 추가한다.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


outbox_status = postgresql.ENUM(
    "READY",
    "PROCESSING",
    "SENT",
    "FAIL",
    name="outbox_status",
    create_type=False,
)


def upgrade() -> None:
    """완료 답변과 Slack API 발신 사이를 잇는 멱등 Outbox를 생성한다."""
    op.create_table(
        "slack_reply_outbox",
        sa.Column("reply_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.String(length=100), nullable=False),
        sa.Column("thread_ts", sa.String(length=50), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("status", outbox_status, server_default="READY", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(length=100), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("slack_message_ts", sa.String(length=50), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["ai_job.job_id"],
            name="fk_slack_reply_outbox_job_id_ai_job",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("reply_id", name="pk_slack_reply_outbox"),
        sa.UniqueConstraint("job_id", name="uq_slack_reply_outbox_job_id"),
    )
    op.create_index(
        "ix_slack_reply_outbox_due",
        "slack_reply_outbox",
        ["status", "next_retry_at", "locked_at"],
    )


def downgrade() -> None:
    """Slack 답변 Outbox와 발행 대상 조회 인덱스를 제거한다."""
    op.drop_index("ix_slack_reply_outbox_due", table_name="slack_reply_outbox")
    op.drop_table("slack_reply_outbox")
