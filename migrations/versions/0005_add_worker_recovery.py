"""Worker 재시도, DLQ 발행 확인, 수동 재처리 원장을 추가한다.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-23
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def upgrade() -> None:
    """복구 Scheduler와 멱등 수동 재처리에 필요한 DB 상태를 추가한다."""
    op.add_column(
        "ai_job",
        sa.Column("dlq_attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "ai_job",
        sa.Column("dlq_last_error", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "ai_job",
        sa.Column("dlq_next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ai_job",
        sa.Column("dlq_published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_ai_job_retry_due",
        "ai_job",
        ["status", "next_retry_at"],
    )
    op.create_index(
        "ix_ai_job_processing_lease",
        "ai_job",
        ["status", "locked_at"],
    )
    op.create_index(
        "ix_ai_job_dlq_pending",
        "ai_job",
        ["status", "dlq_published_at", "dlq_next_retry_at"],
    )
    op.create_table(
        "job_recovery_request",
        sa.Column(
            "recovery_request_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("from_status", job_status, nullable=False),
        sa.Column("resulting_workflow_revision", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["ai_job.job_id"],
            name="fk_job_recovery_request_job_id_ai_job",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "recovery_request_id", name="pk_job_recovery_request"
        ),
        sa.UniqueConstraint(
            "job_id",
            "idempotency_key",
            name="uq_job_recovery_request_job_key",
        ),
    )


def downgrade() -> None:
    """수동 재처리 원장과 Worker 복구용 열·인덱스를 제거한다."""
    op.drop_table("job_recovery_request")
    op.drop_index("ix_ai_job_dlq_pending", table_name="ai_job")
    op.drop_index("ix_ai_job_processing_lease", table_name="ai_job")
    op.drop_index("ix_ai_job_retry_due", table_name="ai_job")
    op.drop_column("ai_job", "dlq_published_at")
    op.drop_column("ai_job", "dlq_next_retry_at")
    op.drop_column("ai_job", "dlq_last_error")
    op.drop_column("ai_job", "dlq_attempt_count")
