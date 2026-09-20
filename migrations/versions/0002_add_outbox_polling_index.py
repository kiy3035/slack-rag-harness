"""Outbox 선점과 오래된 Lease 복구 조회를 위한 인덱스를 추가한다.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """READY 재시도와 PROCESSING Lease 복구 조회를 인덱스로 지원한다."""
    op.create_index(
        "ix_job_outbox_ready_poll",
        "job_outbox",
        ["status", "next_retry_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_job_outbox_processing_lease",
        "job_outbox",
        ["status", "locked_at"],
        unique=False,
    )


def downgrade() -> None:
    """2단계 Outbox 조회 인덱스를 제거한다."""
    op.drop_index("ix_job_outbox_processing_lease", table_name="job_outbox")
    op.drop_index("ix_job_outbox_ready_poll", table_name="job_outbox")

