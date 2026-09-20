"""사람 검토와 최종 답변 인용 테이블을 생성한다.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-20
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


review_status = postgresql.ENUM(
    "WAITING",
    "APPROVED",
    "EDITED",
    "RETRY_REQUESTED",
    "REJECTED",
    name="review_status",
    create_type=False,
)


def upgrade() -> None:
    """Workflow 실행 세대와 검토·인용 영속화 스키마를 추가한다."""
    review_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "ai_job",
        sa.Column(
            "workflow_revision", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.create_table(
        "review_queue",
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_revision", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("draft_answer", sa.Text(), nullable=True),
        sa.Column(
            "draft_citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "allowed_citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", review_status, nullable=False),
        sa.Column("review_comment", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["ai_job.job_id"],
            name="fk_review_queue_job_id_ai_job",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("review_id", name="pk_review_queue"),
        sa.UniqueConstraint(
            "job_id",
            "workflow_revision",
            name="uq_review_queue_job_revision",
        ),
    )
    op.create_index(
        "ix_review_queue_status_created",
        "review_queue",
        ["status", "created_at"],
    )
    op.create_table(
        "answer_citation",
        sa.Column("citation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("similarity_score", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "similarity_score >= -1 AND similarity_score <= 1",
            name="ck_answer_citation_similarity_range",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["ai_job.job_id"],
            name="fk_answer_citation_job_id_ai_job",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_document.document_id"],
            name="fk_answer_citation_document_id_knowledge_document",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["knowledge_chunk.chunk_id"],
            name="fk_answer_citation_chunk_id_knowledge_chunk",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("citation_id", name="pk_answer_citation"),
        sa.UniqueConstraint(
            "job_id", "chunk_id", name="uq_answer_citation_job_chunk"
        ),
    )


def downgrade() -> None:
    """답변 인용과 검토 스키마 및 실행 세대 열을 제거한다."""
    op.drop_table("answer_citation")
    op.drop_index("ix_review_queue_status_created", table_name="review_queue")
    op.drop_table("review_queue")
    op.drop_column("ai_job", "workflow_revision")
    review_status.drop(op.get_bind(), checkfirst=True)
